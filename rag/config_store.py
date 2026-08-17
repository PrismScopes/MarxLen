"""
配置中心 —— 全项目所有可自定义配置的单一数据源

设计目标：
  1. **单一数据源**：每个配置项只在 CONFIG_SCHEMA 里声明一次，
     默认值、类型、取值范围、前端渲染元数据全部由此派生。
     新增一个配置项只需追加一条 ConfigItem，无需改动任何其他地方。
  2. **持久化**：用户改动写入 config.json，重启不丢失；
     只保存被修改过的项，未改动的项始终跟随代码里的默认值演进。
  3. **前端友好**：get_schema() 直接返回带 label/描述/类型/范围/分组的
     完整元数据，前端可据此自动渲染设置页面，无需硬编码任何字段。
  4. **健壮**：非法值自动钳制或回退默认值，配置文件损坏不影响服务启动。
  5. **安全**：API Key 一类敏感项只存 .env，下发前脱敏。

配置分三种存储位置（由 ConfigItem.store 指定）：
  - "json"    → config.json，可热更新，重启保留（大多数配置）
  - "env"     → rag/.env，涉及密钥与服务端点
  - "runtime" → 仅当前进程内存，重启复位
"""

import os
import re
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

import portalocker
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

RAG_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(RAG_DIR)
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
ENV_PATH = os.path.join(RAG_DIR, ".env")


# ======================================================================
# 配置项定义
# ======================================================================

@dataclass
class ConfigItem:
    """单个配置项的完整规格

    字段说明：
        key         配置键，全局唯一
        label       前端显示名称
        type        控件类型：text/textarea/password/int/number/boolean/select
        default     默认值
        category    前端分组，须对应 CATEGORIES 中的 id
        description 说明文字，显示在控件下方
        options     select 类型的候选项
        min / max   数值范围，超出会被钳制
        step        数值步进，供前端 slider/input 使用
        unit        单位，前端显示在输入框后（如"秒"、"条"）
        section     分类内的小节标题（可选），前端在同一分类下渲染
                    小节分隔，用于收纳多个子主题（如"系统"下的
                    对话/缓存/服务）
        store       存储位置：json / env / runtime
        env_key     store=="env" 时对应的环境变量名
        env_aliases 兼容的旧环境变量名，读取时按顺序回退（写入只用 env_key）
        secret      敏感项，下发前脱敏
        advanced    高级选项，前端可默认折叠
        requires_restart  修改后需重启服务才生效
    """
    key: str
    type: str = "text"
    default: Any = ""
    label: str = ""
    category: str = "general"
    description: str = ""
    options: Optional[List[Union[str, Dict[str, str]]]] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    unit: str = ""
    section: str = ""
    store: str = "json"
    env_key: Optional[str] = None
    env_aliases: Optional[List[str]] = None
    secret: bool = False
    advanced: bool = False
    requires_restart: bool = False

    # ── 类型转换 ────────────────────────────────────────
    def cast(self, value: Any) -> Any:
        """把任意外部输入转换为本项声明的类型

        设计原则：**永不抛异常**。配置项的取值来自用户输入或手工编辑的
        文件，非法值应当被钳制或回退到默认值，而不是让服务崩溃。
        """
        if value is None:
            return self.default

        if self.type == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "1", "yes", "on"):
                    return True
                if low in ("false", "0", "no", "off", ""):
                    return False
            return self.default

        if self.type == "int":
            try:
                num = int(float(str(value).strip()))
            except (TypeError, ValueError):
                return self.default
            return self._clamp(num)

        if self.type == "number":
            try:
                num = float(str(value).strip())
            except (TypeError, ValueError):
                return self.default
            return self._clamp(num)

        if self.type == "select":
            text = str(value).strip()
            allowed = self.option_values()
            # 空 options 表示候选项由运行时动态提供（如模型列表），不做校验
            if allowed and text not in allowed:
                return self.default
            return text

        # text / textarea / password
        return str(value).strip()

    def _clamp(self, num):
        """把数值钳制到 [min, max] 区间内"""
        if self.min is not None and num < self.min:
            return type(num)(self.min)
        if self.max is not None and num > self.max:
            return type(num)(self.max)
        return num

    def option_values(self) -> List[str]:
        """提取 select 的合法取值列表"""
        if not self.options:
            return []
        return [o["value"] if isinstance(o, dict) else str(o) for o in self.options]


# 前端设置页面的分组（顺序即显示顺序）。
# 按"用户使用场景"组织:模型怎么配、检索怎么调、回答怎么生成、
# 密钥放哪、阅读器、剩下的系统项。
CATEGORIES = [
    {"id": "model",     "label": "模型与推理",   "icon": "cpu",
     "description": "对话模型、候选列表与生成参数"},
    {"id": "retrieval", "label": "检索与知识库", "icon": "search",
     "description": "召回策略、融合重排与知识库版本管理"},
    {"id": "prompt",    "label": "回答与提示词", "icon": "file-text",
     "description": "思考流程、输出规范与检索前置分析"},
    {"id": "api",       "label": "接口与密钥",   "icon": "plug",
     "description": "对话/嵌入/重排三套端点、密钥与超时"},
    {"id": "reader",    "label": "原文阅读器",   "icon": "book-open",
     "description": "语料目录、阅读分页与原文检索"},
    {"id": "system",    "label": "系统",         "icon": "server",
     "description": "对话上下文、缓存与服务参数"},
]


# ======================================================================
# 全部配置项（唯一数据源）
# 新增配置只需在此追加一条，设置页面会自动出现对应控件
# ======================================================================

CONFIG_SCHEMA: List[ConfigItem] = [

    # ── 模型 ────────────────────────────────────────────────
    ConfigItem(
        key="model", type="select", default="deepseek-chat",
        label="对话模型", category="model",
        description="用于生成最终回答的大语言模型",
        store="env", env_key="OPENAI_MODEL",
        env_aliases=["DEEPSEEK_MODEL"],
    ),
    ConfigItem(
        key="temperature", type="number", default=0.3,
        label="温度", category="model",
        description="控制回答的发散程度。偏低更严谨稳定，偏高更有创造性",
        min=0.0, max=2.0, step=0.05,
    ),
    ConfigItem(
        key="max_tokens", type="int", default=0,
        label="回答长度上限", category="model", unit="token",
        description="单次回答的最大 token 数。填 0 表示不限制，由模型自行决定",
        min=0, max=32000, step=256,
    ),
    ConfigItem(
        key="top_p", type="number", default=1.0,
        label="Top-P 采样", category="model",
        description="核采样阈值。与温度通常只调其中一个",
        min=0.0, max=1.0, step=0.05, advanced=True,
    ),
    ConfigItem(
        key="thinking_effort", type="select", default="off",
        label="默认思考强度", category="model",
        options=[
            {"value": "off", "label": "关闭（秒回）"},
            {"value": "high", "label": "标准思考"},
            {"value": "max", "label": "深度思考"},
        ],
        description="新对话默认的推理强度。off 不启用推理模型思考；"
                    "high / max 映射为 DeepSeek 的 reasoning_effort，"
                    "思考越深耗时越长，需推理模型支持",
    ),
    ConfigItem(
        key="model_list", type="textarea", default="",
        label="可选模型列表", category="model",
        description="模型选择器与设置页下拉的候选项，"
                    "格式：模型ID:显示名，多个用英文逗号分隔。"
                    "留空则只显示当前模型",
        store="env", env_key="OPENAI_MODEL_LIST",
        env_aliases=["DEEPSEEK_MODEL_LIST"],
    ),

    # ── 接口与密钥 ──────────────────────────────────────────
    ConfigItem(
        key="api_base_url", type="text", default="https://api.deepseek.com/v1",
        label="对话模型 API 地址", category="api",
        description="兼容 OpenAI 格式的端点地址。保存后立即生效",
        store="env", env_key="OPENAI_API_BASE_URL",
        env_aliases=["DEEPSEEK_API_BASE_URL"],
    ),
    ConfigItem(
        key="api_key", type="password", default="",
        label="对话模型 API Key", category="api",
        description="留空表示不修改，沿用 .env 中已配置的值。保存后立即生效",
        store="env", env_key="OPENAI_API_KEY",
        env_aliases=["DEEPSEEK_API_KEY"], secret=True,
    ),
    ConfigItem(
        key="embed_api_base_url", type="text", default="https://api2.aigcbest.top/v1",
        label="嵌入/重排 API 地址", category="api",
        description="Embedding 与 Rerank 共用的端点地址。保存后立即生效",
        store="env", env_key="EMBED_API_BASE_URL",
    ),
    ConfigItem(
        key="embed_api_key", type="password", default="",
        label="嵌入/重排 API Key", category="api",
        description="留空表示不修改，沿用 .env 中已配置的值。保存后立即生效",
        store="env", env_key="EMBED_API_KEY", env_aliases=["RERANK_API_KEY"],
        secret=True,
    ),
    ConfigItem(
        key="embed_model", type="text", default="Qwen/Qwen3-Embedding-0.6B",
        label="嵌入模型", category="api",
        description="更换后必须重建索引，否则向量维度不匹配（需重启服务）",
        store="env", env_key="EMBED_MODEL", requires_restart=True,
    ),
    ConfigItem(
        key="rerank_model", type="text", default="Qwen/Qwen3-Reranker-4B",
        label="重排模型", category="api",
        description="用于精排候选文档的 Rerank 模型。保存后立即生效",
        store="env", env_key="RERANK_MODEL",
    ),
    ConfigItem(
        key="rerank_timeout", type="int", default=30,
        label="重排超时", category="api", unit="秒",
        description="超时后降级为使用 RRF 融合序，不影响回答生成",
        min=5, max=180, step=5,
        store="env", env_key="RERANK_TIMEOUT",
    ),
    ConfigItem(
        key="embed_timeout", type="int", default=60,
        label="嵌入超时", category="api", unit="秒",
        description="单次嵌入请求的读取超时。保存后立即生效",
        min=5, max=300, step=5, advanced=True,
    ),
    ConfigItem(
        key="embed_max_retries", type="int", default=3,
        label="嵌入重试次数", category="api", unit="次",
        description="嵌入请求失败后的自动重试次数。保存后立即生效",
        min=0, max=10, step=1, advanced=True,
    ),

    # ── 检索 ────────────────────────────────────────────────
    ConfigItem(
        key="top_k", type="int", default=8,
        label="参考文档数", category="retrieval", unit="条",
        description="重排序后送入模型并展示给用户的文档数量",
        min=1, max=50, step=1,
    ),
    ConfigItem(
        key="context_max_chars", type="int", default=3500,
        label="参考文档总字数上限", category="retrieval", unit="字",
        description="送入模型的全部参考文档累计字数上限。超出时按重排分数"
                    "分配预算：分数越高保留越完整，低分文档只保留关键句。"
                    "防止长上下文稀释模型对高相关文档的注意力",
        min=800, max=12000, step=200,
    ),
    ConfigItem(
        key="fetch_k", type="int", default=30,
        label="候选池大小", category="retrieval", unit="条",
        description="每个检索通道的召回数量。越大越全面，但重排序耗时增加",
        min=5, max=200, step=5,
    ),
    ConfigItem(
        key="enable_query_planner", type="boolean", default=True,
        label="启用检索前置分析", category="retrieval",
        description="用大模型先解构问题再检索，显著提升召回质量，"
                    "但每次提问会多一次模型调用",
    ),
    ConfigItem(
        key="enable_multi_channel", type="boolean", default=True,
        label="启用多通道检索", category="retrieval",
        description="将多个假设性命题分别向量检索后融合（HyDE 增强）。"
                    "关闭则退化为单查询检索",
    ),
    ConfigItem(
        key="enable_retrieval_check", type="boolean", default=True,
        label="启用检索自检与补检", category="retrieval",
        description="检索结果覆盖度不足(来源文件过少或无重排分)时，"
                    "用核心矛盾定向补一轮检索合并进上下文，"
                    "提升多子问题提问的召回完整性",
    ),
    ConfigItem(
        key="max_propositions", type="int", default=3,
        label="语义通道数", category="retrieval", unit="条",
        description="前置分析最多生成几条命题，每条对应一个向量检索通道",
        min=1, max=5, step=1,
    ),
    ConfigItem(
        key="enable_reranker", type="boolean", default=True,
        label="启用重排序", category="retrieval",
        description="调用 Rerank 模型精排候选文档。关闭可省一次 API 调用，"
                    "但排序质量会明显下降",
    ),
    ConfigItem(
        key="rrf_k", type="int", default=60,
        label="RRF 平滑常数", category="retrieval",
        description="倒数排名融合的平滑参数。越小越突出各通道的头部结果",
        min=1, max=200, step=1, advanced=True,
    ),
    ConfigItem(
        key="score_threshold", type="number", default=0.0,
        label="相关度阈值", category="retrieval",
        description="重排序得分低于此值的文档会被丢弃。0 表示不过滤",
        min=0.0, max=1.0, step=0.01, advanced=True,
        store="env", env_key="SCORE_THRESHOLD",
    ),
    ConfigItem(
        key="dedup_prefix_len", type="int", default=200,
        label="去重比对长度", category="retrieval", unit="字",
        description="按文本前 N 字判断是否为重复片段",
        min=50, max=1000, step=50, advanced=True,
    ),
    ConfigItem(
        key="excerpt_len", type="int", default=300,
        label="来源摘录长度", category="retrieval", unit="字",
        description="来源卡片中展示的原文摘录字数",
        min=100, max=1000, step=50,
    ),

    # ── 提示词 ──────────────────────────────────────────────
    ConfigItem(
        key="main_prompt_file", type="text", default="main_prompt.txt",
        label="主提示词文件", category="prompt",
        description="Prompt/ 目录下的文件名，定义 AI 的思考方法与输出规范",
        requires_restart=True,
    ),
    ConfigItem(
        key="rag_prompt_file", type="text", default="RAG_prompt.txt",
        label="检索提示词文件", category="prompt",
        description="Prompt/ 目录下的文件名，定义问题解构与检索计划生成",
        requires_restart=True,
    ),
    ConfigItem(
        key="planner_model", type="text", default="deepseek-chat",
        label="前置分析模型", category="prompt",
        description="前置分析只做结构化抽取，用快模型即可。"
                    "推理型模型会为此多花数倍时间（实测 9.8s vs 3.5s）。"
                    "留空则跟随回答所用的模型",
    ),
    ConfigItem(
        key="planner_temperature", type="number", default=0.2,
        label="前置分析温度", category="prompt",
        description="结构化抽取需要稳定输出，建议保持较低值",
        min=0.0, max=1.0, step=0.05, advanced=True,
    ),
    ConfigItem(
        key="planner_max_tokens", type="int", default=1500,
        label="前置分析长度上限", category="prompt", unit="token",
        description="过小会导致 JSON 被截断而解析失败，从而降级为单查询检索",
        min=500, max=4000, step=100, advanced=True,
    ),
    ConfigItem(
        key="planner_timeout", type="int", default=40,
        label="前置分析超时", category="prompt", unit="秒",
        description="超时后自动降级为直接用原问题检索",
        min=5, max=180, step=5, advanced=True,
    ),
    ConfigItem(
        key="planner_min_question_len", type="int", default=8,
        label="触发分析的最短问题长度", category="prompt", unit="字",
        description="短于此长度的问题跳过前置分析，直接检索以节省调用",
        min=0, max=100, step=1, advanced=True,
    ),

    # ── 系统:对话 ───────────────────────────────────────────
    ConfigItem(
        key="history_turns", type="int", default=20,
        label="上下文轮数", category="system", section="对话", unit="条",
        description="携带最近多少条历史消息作为上下文",
        min=0, max=100, step=1,
    ),
    ConfigItem(
        key="history_msg_len", type="int", default=500,
        label="单条历史截断长度", category="system", section="对话", unit="字",
        description="每条历史消息保留的字数，用于控制 token 消耗",
        min=100, max=2000, step=100,
    ),
    ConfigItem(
        key="conversation_list_limit", type="int", default=50,
        label="历史列表条数", category="system", section="对话", unit="条",
        description="侧边栏一次加载的对话数量",
        min=10, max=500, step=10,
    ),
    ConfigItem(
        key="title_len", type="int", default=20,
        label="标题截取长度", category="system", section="对话", unit="字",
        description="用问题的前 N 字作为新对话的标题",
        min=5, max=100, step=5,
    ),
    ConfigItem(
        key="default_mode", type="select", default="general",
        label="默认问答模式", category="system", section="对话",
        options=[
            {"value": "general",     "label": "通用问答"},
            {"value": "methodology", "label": "马哲方法论"},
            {"value": "original",    "label": "原文查询"},
        ],
        description="新对话启动时使用的模式",
    ),
    ConfigItem(
        key="web_search_results", type="int", default=5,
        label="联网搜索结果数", category="system", section="对话", unit="条",
        description="启用联网搜索时抓取的网页数量",
        min=1, max=20, step=1,
    ),
    ConfigItem(
        key="web_search_excerpt", type="int", default=200,
        label="搜索摘要长度", category="system", section="对话", unit="字",
        description="每条网页结果保留的摘要字数",
        min=50, max=1000, step=50, advanced=True,
    ),

    # ── 系统:缓存 ───────────────────────────────────────────
    ConfigItem(
        key="enable_answer_cache", type="boolean", default=True,
        label="启用回答缓存", category="system", section="缓存",
        description="相同问题直接返回历史回答，秒级响应且不消耗额度",
    ),
    ConfigItem(
        key="enable_embed_cache", type="boolean", default=True,
        label="启用向量缓存", category="system", section="缓存",
        description="缓存查询的嵌入向量，避免重复调用嵌入 API",
    ),
    ConfigItem(
        key="max_answer_entries", type="int", default=500,
        label="回答缓存上限", category="system", section="缓存", unit="条",
        description="超出后自动淘汰最旧的记录",
        min=0, max=100000, step=100,
    ),
    ConfigItem(
        key="max_embed_entries", type="int", default=2000,
        label="向量缓存上限", category="system", section="缓存", unit="条",
        description="超出后自动淘汰最旧的记录",
        min=0, max=100000, step=100,
    ),

    # ── 系统:服务 ───────────────────────────────────────────
    ConfigItem(
        key="stream_timeout", type="int", default=60,
        label="流式响应超时", category="system", section="服务", unit="秒",
        description="模型持续无输出超过此时长则中断本次流",
        min=10, max=600, step=10, advanced=True,
    ),
    ConfigItem(
        key="stream_queue_size", type="int", default=128,
        label="流式队列容量", category="system", section="服务",
        description="SSE 推送队列长度，用于背压控制",
        min=16, max=1024, step=16, advanced=True,
    ),
    ConfigItem(
        key="cors_origins", type="textarea", default="",
        label="允许的跨域来源", category="system", section="服务",
        description="多个用英文逗号分隔。留空则只允许本机访问",
        store="env", env_key="CORS_ALLOW_ORIGINS", requires_restart=True,
    ),
    ConfigItem(
        key="log_level", type="select", default="INFO",
        label="日志级别", category="system", section="服务",
        options=["DEBUG", "INFO", "WARNING", "ERROR"],
        description="DEBUG 会输出检索细节，便于排查问题但日志量大",
        requires_restart=True,
    ),

    # ── 原文阅读器 ──────────────────────────────────────────
    ConfigItem(
        key="reader_enabled", type="boolean", default=True,
        label="启用原文阅读器", category="reader",
        description="关闭后原文查询模式不可用。原文目录缺失时会自动降级",
    ),
    ConfigItem(
        key="reader_corpus_dir", type="text", default="ww",
        label="原文目录", category="reader",
        description="存放原始 Markdown 的目录。相对路径以项目根为基准",
        store="env", env_key="WW_DIR", requires_restart=True,
    ),
    ConfigItem(
        key="reader_page_chars", type="int", default=20000,
        label="单页正文字数", category="reader", unit="字",
        description="阅读器每次请求返回的正文字数，过大会拖慢首屏",
        min=2000, max=100000, step=1000, advanced=True,
    ),
    ConfigItem(
        key="reader_probe_len", type="int", default=60,
        label="定位探针长度", category="reader", unit="字",
        description="用片段前 N 字回原文定位。太短易撞见多处，太长易受换行干扰",
        min=20, max=200, step=10, advanced=True,
    ),
    ConfigItem(
        key="reader_search_keywords", type="int", default=5,
        label="模糊搜索关键词数", category="reader", unit="个",
        description="阅读器模糊搜索时，让模型从描述中提取几个经典术语",
        min=1, max=10, step=1, advanced=True,
    ),
    ConfigItem(
        key="reader_search_top_k", type="int", default=10,
        label="模糊搜索结果数", category="reader", unit="条",
        description="阅读器模糊搜索返回多少条候选片段",
        min=3, max=30, step=1,
    ),

    # ── 知识库版本管理（离线数据工程，归入检索与知识库） ─────
    ConfigItem(
        key="kb_enabled", type="boolean", default=True,
        label="启用版本化知识库", category="retrieval",
        description="启动时从 data/releases.json 解析当前知识库版本。"
                    "尚无发布记录时自动回退到 rag/ 目录的传统索引",
    ),
    ConfigItem(
        key="kb_hot_reload", type="boolean", default=True,
        label="知识库热更新", category="retrieval",
        description="检测到新版本发布后，后台加载新索引并原子切换，"
                    "无需重启服务",
    ),
    ConfigItem(
        key="kb_builds_keep", type="int", default=3,
        label="保留构建版本数", category="retrieval", unit="个",
        description="kb gc 清理时保留最近多少个构建目录",
        min=1, max=20, step=1,
    ),
]

_ITEM_BY_KEY: Dict[str, ConfigItem] = {it.key: it for it in CONFIG_SCHEMA}


def _mask(value: str) -> str:
    """脱敏：只保留前后各 4 位"""
    if not value:
        return ""
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


# ======================================================================
# 配置中心
# ======================================================================

class ConfigStore:
    """配置读写中心

    读取优先级：runtime > config.json > 环境变量 > 默认值
    """

    def __init__(self, config_path: str = CONFIG_PATH, env_path: str = ENV_PATH):
        self.config_path = config_path
        self.env_path = env_path
        self._lock = threading.RLock()
        self._json: Dict[str, Any] = {}
        self._runtime: Dict[str, Any] = {}
        self._load()

    # ── 加载与保存 ──────────────────────────────────────

    def _load(self):
        """载入 config.json。文件不存在或损坏时使用默认值，不阻断启动。"""
        if not os.path.exists(self.config_path):
            self._json = {}
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # 忽略 _comment 之类的说明性字段
                self._json = {k: v for k, v in data.items() if not k.startswith("_")}
                logger.info(f"已加载配置: {self.config_path}（{len(self._json)} 项自定义）")
            else:
                logger.warning("配置文件顶层应为对象，已忽略")
                self._json = {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"配置文件读取失败，使用默认配置: {e}")
            self._json = {}

    def _save(self):
        """原子写入：先写临时文件再替换，避免中断导致文件损坏"""
        payload = {
            "_comment": "MarxLen 用户配置。只记录被修改过的项，"
                        "删除某项即恢复其默认值。也可直接编辑本文件。",
            **self._json,
        }
        tmp = self.config_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.config_path)
        except OSError as e:
            logger.error(f"配置保存失败: {e}")

    def reload(self):
        """重新从磁盘载入（外部直接编辑了配置文件后调用）"""
        with self._lock:
            self._load()
        return self

    # ── 读取 ────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """读取配置项的生效值。未知 key 返回 default。"""
        item = _ITEM_BY_KEY.get(key)
        if item is None:
            return default

        with self._lock:
            if key in self._runtime:
                return item.cast(self._runtime[key])

            if item.store == "json" and key in self._json:
                return item.cast(self._json[key])

            if item.store == "env" and item.env_key:
                # 主键优先，其次按顺序尝试历史别名
                for env_name in [item.env_key, *(item.env_aliases or [])]:
                    raw = os.getenv(env_name)
                    if raw not in (None, ""):
                        return item.cast(raw)

            return item.default

    def get_all(self) -> Dict[str, Any]:
        """所有配置项的生效值（含敏感项真实值，仅供服务端内部使用）"""
        return {it.key: self.get(it.key) for it in CONFIG_SCHEMA}

    def get_schema(self) -> Dict[str, Any]:
        """导出给前端的完整设置描述

        前端据此自动渲染设置页面：categories 决定标签页，
        items 决定每个控件的类型、范围、说明与当前值。
        """
        items = []
        for it in CONFIG_SCHEMA:
            value = self.get(it.key)
            if it.secret:
                real = str(value or "")
                shown, is_set = _mask(real), bool(real)
            else:
                shown, is_set = value, True

            items.append({
                "key": it.key,
                "label": it.label or it.key,
                "type": it.type,
                "value": shown,
                "default": it.default,
                "category": it.category,
                "description": it.description,
                "options": it.options,
                "min": it.min,
                "max": it.max,
                "step": it.step,
                "unit": it.unit,
                "section": it.section,
                "secret": it.secret,
                "is_set": is_set,
                "advanced": it.advanced,
                "requires_restart": it.requires_restart,
                "is_default": value == it.default,
            })
        return {"categories": CATEGORIES, "items": items}

    # ── 写入 ────────────────────────────────────────────

    def update(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """批量更新配置，返回更新后的全量生效值

        未知 key 会被忽略；非法值按 cast 规则钳制或回退，不会抛异常。
        """
        if not isinstance(updates, dict):
            return self.get_all()

        env_updates: Dict[str, str] = {}
        json_changed = False

        with self._lock:
            for key, raw in updates.items():
                item = _ITEM_BY_KEY.get(key)
                if item is None:
                    continue
                # 敏感项传空表示"不修改"，避免前端回显脱敏值时误清空
                if item.secret and (raw is None or str(raw).strip() == ""):
                    continue

                value = item.cast(raw)
                if item.store == "env" and item.env_key:
                    env_updates[item.env_key] = str(value)
                elif item.store == "runtime":
                    self._runtime[key] = value
                else:
                    self._json[key] = value
                    json_changed = True

            if json_changed:
                self._save()

        if env_updates:
            self._write_env(env_updates)

        return self.get_all()

    def reset(self, key: Optional[str] = None) -> Dict[str, Any]:
        """恢复默认值。key 为空则重置全部 json 配置。

        .env 中的端点与密钥不参与重置，避免误删用户凭据。
        """
        with self._lock:
            if key is None:
                self._json.clear()
                self._runtime.clear()
            else:
                self._json.pop(key, None)
                self._runtime.pop(key, None)
            self._save()
        return self.get_all()

    def _write_env(self, env_updates: Dict[str, str]):
        """写入 .env，加排他锁防止并发写坏文件"""
        if not os.path.exists(self.env_path):
            logger.warning(f".env 不存在，跳过写入: {self.env_path}")
            return
        try:
            with portalocker.Lock(self.env_path, mode="r+", encoding="utf-8",
                                  flags=portalocker.LOCK_EX) as f:
                content = f.read()
                for env_key, value in env_updates.items():
                    pattern = rf"^{re.escape(env_key)}=.*"
                    if re.search(pattern, content, re.MULTILINE):
                        content = re.sub(pattern, f"{env_key}={value}",
                                         content, flags=re.MULTILINE)
                    else:
                        content = content.rstrip("\n") + f"\n{env_key}={value}\n"
                f.seek(0)
                f.write(content)
                f.truncate()
            load_dotenv(self.env_path, override=True)
            logger.info(f"已更新 .env: {list(env_updates.keys())}")
        except OSError as e:
            logger.error(f"写入 .env 失败: {e}")


# ── 全局单例 ────────────────────────────────────────────

_config: Optional[ConfigStore] = None
_config_lock = threading.Lock()


def get_config() -> ConfigStore:
    """获取全局配置实例（懒加载单例）"""
    global _config
    if _config is None:
        with _config_lock:
            if _config is None:
                _config = ConfigStore()
    return _config


def reload_config() -> ConfigStore:
    """重新从磁盘载入配置"""
    return get_config().reload()
