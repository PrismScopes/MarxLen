"""
提示词加载与检索前置分析模块

职责：
  1. 从 Prompt/ 目录加载外部提示词文件（main_prompt.txt / RAG_prompt.txt），
     使提示词可以脱离代码独立迭代
  2. 调用 LLM 执行 RAG_prompt 定义的"认知增强处理"，把用户白话问题
     解构为结构化的检索计划（QueryPlan）

设计说明：
  提示词放在项目根的 Prompt/ 目录而非硬编码在代码里，改提示词无需改代码。
  文件缺失时回退到内置的精简版本，保证服务不因缺文件而起不来。
"""

import os
import re
import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .config_store import get_config

logger = logging.getLogger(__name__)

# Prompt 目录位于项目根目录下（rag/ 的上一级）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_DIR = os.path.join(_PROJECT_ROOT, "Prompt")

# 提示词文件缺失时的兜底内容（保持核心约束，避免服务不可用）
_FALLBACK_MAIN_PROMPT = """你是一位遵循唯物辩证法思考方法的分析者。
一切认识从客观实际出发，在事物的内在矛盾、普遍联系和发展变化中把握问题。

你将收到前置检索模块提供的结构化数据（学科定位、核心矛盾、参考文档等），
将其作为分析的事实基础。

输出要求：
- 只输出成品回答，不展示内部思考步骤
- 使用了参考文档的段落，末尾另起一行标注 `> 参考自《著作名》`，
  引用行前后各空一行，引用行内只写这一句话
- 禁止使用"[来源 N]"编号引用，禁止把引用堆在文末
- 禁止回答"资料中没有相关信息"
"""


def _read_prompt_file(path: str, fallback: str = "") -> str:
    """读取提示词文件，失败时返回兜底内容"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            logger.info(f"已加载提示词: {os.path.basename(path)}（{len(content)} 字）")
            return content
        logger.warning(f"提示词文件为空: {path}")
    except FileNotFoundError:
        logger.warning(f"提示词文件不存在: {path}，使用内置兜底提示词")
    except OSError as e:
        logger.warning(f"提示词文件读取失败: {path} - {e}")
    return fallback


def load_main_prompt() -> str:
    """加载主思考提示词（作为 system message 使用）

    文件名由设置项 main_prompt_file 指定，只取 basename，
    避免通过 ../ 越出 Prompt 目录读到任意文件。
    """
    name = os.path.basename(get_config().get("main_prompt_file"))
    return _read_prompt_file(os.path.join(PROMPT_DIR, name), _FALLBACK_MAIN_PROMPT)


def load_rag_prompt() -> str:
    """加载检索前置处理提示词（含 {question} 占位符）"""
    name = os.path.basename(get_config().get("rag_prompt_file"))
    return _read_prompt_file(os.path.join(PROMPT_DIR, name), "")


# ======================================================================
# 检索计划
# ======================================================================

@dataclass
class QueryPlan:
    """RAG_prompt 解构用户问题后产出的结构化检索计划

    字段与 RAG_prompt.txt 的输出格式一一对应。
    analysis_ok 标记 LLM 分析是否成功，失败时各字段为降级默认值，
    检索流程退回到"直接用原问题检索"。
    """
    question: str
    domain: str = ""                 # 学科定位
    level: str = ""                  # 范畴层级
    nature: str = ""                 # 问题性质
    core_contradiction: str = ""     # 核心矛盾
    propositions: List[str] = field(default_factory=list)  # 假设性命题（向量检索用）
    keywords: List[str] = field(default_factory=list)      # 经典范畴（BM25 用）
    missing_perspective: str = ""    # 缺失视角提示
    analysis_ok: bool = False        # 分析是否成功

    # ── 供检索器使用 ──────────────────────────────────────

    def dense_queries(self) -> List[str]:
        """稠密检索使用的查询串：假设性命题（HyDE 增强）

        命题是完整陈述句，语义上更接近经典原文的表述方式，
        比用户白话更容易召回到对应章节。
        """
        queries = [p.strip() for p in self.propositions if p and p.strip()]
        return queries or [self.question]

    def sparse_query(self) -> str:
        """稀疏检索使用的查询串：经典范畴关键词

        BM25 依赖词面匹配，用理论术语比用日常词汇命中率高得多。
        """
        kws = [k.strip() for k in self.keywords if k and k.strip()]
        return " ".join(kws) if kws else self.question

    # ── 供主提示词使用 ────────────────────────────────────

    def to_context_block(self) -> str:
        """渲染为主思考模块可读的结构化输入块

        字段名与 main_prompt.txt 的输入数据契约表格保持一致。
        """
        if not self.analysis_ok:
            # 分析失败时不输出半成品字段，避免给模型错误的认知锚点
            return f"【原始问题】：{self.question}\n"

        lines = ["【前置检索模块输出】", f"  原始问题：{self.question}"]
        if self.domain:
            lines.append(f"  学科定位：{self.domain}")
        if self.level:
            lines.append(f"  范畴层级：{self.level}")
        if self.nature:
            lines.append(f"  问题性质：{self.nature}")
        if self.core_contradiction:
            lines.append(f"  核心矛盾：{self.core_contradiction}")
        if self.propositions:
            lines.append("  经典命题映射：")
            for p in self.propositions:
                lines.append(f"    - {p}")
        if self.keywords:
            lines.append(f"  锚定范畴：{'、'.join(self.keywords)}")
        if self.missing_perspective:
            # 标签措辞刻意避开"缺失"二字：模型容易把带"缺失"的标签
            # 原样复述进回答（实测出现过），换成祈使句式可显著降低泄露概率
            lines.append(f"  需补充展开的视角：{self.missing_perspective}")
        return "\n".join(lines) + "\n"


def _extract_json(raw: str) -> Optional[dict]:
    """从 LLM 输出中提取 JSON 对象

    模型有时会违反指令包上 ```json 代码块或加前后说明文字，
    这里做容错：先剥代码块围栏，再截取第一个 { 到最后一个 } 之间的内容。
    """
    if not raw:
        return None

    text = raw.strip()

    # 剥离 markdown 代码块围栏
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]                      # 去掉 ```json 那一行
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # 截取最外层花括号，容忍前后多余文字
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _as_str_list(value, limit: int = 8) -> List[str]:
    """把 LLM 返回的任意形态字段规整为字符串列表"""
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = [str(v) for v in value if v]
    else:
        return []
    return [s.strip() for s in items if s and s.strip()][:limit]


# 模型常把提示词里的维度标签一起写进命题（如"维度A：马克思关于……"）。
# 这些标签会混入向量查询干扰语义匹配，需在入库前剥掉。
# 覆盖两类形态：
#   1. 括号包裹整体：（对应维度A）命题 / (维度A)命题
#   2. 裸标签加冒号：维度A：命题 / 维度A（经典原理映射）：命题
_DIMENSION_PREFIX = re.compile(
    r"^\s*(?:"
    r"[（(]\s*(?:对应)?\s*维度\s*[A-Za-z一二三四五]\s*(?:[（(][^）)]*[）)])?\s*[）)]"
    r"|(?:对应)?\s*维度\s*[A-Za-z一二三四五]\s*(?:[（(][^）)]*[）)])?\s*[:：]"
    r")\s*[:：]?\s*"
)


def _strip_dimension_label(text: str) -> str:
    """剥离命题开头的"维度X："类标签，保留命题正文"""
    cleaned = _DIMENSION_PREFIX.sub("", text).strip()
    # 清洗后不能把内容清空，否则宁可保留原文
    return cleaned or text.strip()


def build_query_plan(sdk_client, model_name: str, question: str,
                     rag_prompt: str, timeout: Optional[float] = None) -> QueryPlan:
    """执行检索前置分析，产出结构化检索计划

    参数:
        sdk_client: OpenAI 兼容客户端
        model_name: 模型 ID
        question:   用户原始问题
        rag_prompt: RAG_prompt 模板（含 {question} 占位符）
        timeout:    单次分析的超时秒数，超时即降级。
                    不传则取设置项 planner_timeout

    任何异常都不向上抛出，而是返回 analysis_ok=False 的降级计划，
    让检索退回"直接用原问题检索"这条始终可用的路径。
    """
    cfg = get_config()
    plan = QueryPlan(question=question)

    # 开关关闭时直接返回降级计划，省掉一次模型调用
    if not cfg.get("enable_query_planner"):
        return plan

    # 过短的问题不值得花一次 LLM 调用去解构
    if len(question.strip()) < int(cfg.get("planner_min_question_len")) or not rag_prompt:
        return plan

    try:
        # RAG_prompt 用 {question} 作为占位符；用 replace 而非 format，
        # 避免提示词里的 JSON 示例花括号被 format 误当成占位符
        filled = rag_prompt.replace("{question}", question)

        req = {
            "model": model_name,
            "messages": [{"role": "user", "content": filled}],
            # 结构化抽取需要稳定输出，温度默认偏低
            "temperature": cfg.get("planner_temperature"),
            # 命题是完整陈述句、核心矛盾往往较长，默认 1500 才够放下完整 JSON。
            # 给少了会在中途截断，导致 JSON 不闭合而解析失败。
            "max_tokens": int(cfg.get("planner_max_tokens")),
            "timeout": float(timeout if timeout is not None else cfg.get("planner_timeout")),
        }

        try:
            # 优先用 JSON 模式，从协议层保证输出可解析
            resp = sdk_client.chat.completions.create(
                **req, response_format={"type": "json_object"}
            )
        except Exception:
            # 部分 OpenAI 兼容端点不支持 response_format，退回普通模式，
            # 靠 _extract_json 的容错解析兜底
            logger.info("端点不支持 json_object 模式，改用普通模式")
            resp = sdk_client.chat.completions.create(**req)

        raw = resp.choices[0].message.content or ""
        data = _extract_json(raw)

        if data is None:
            logger.warning(f"前置分析未返回可解析 JSON，降级为原问题检索。原始输出: {raw[:120]}")
            return plan

        plan.domain = str(data.get("domain", "") or "").strip()
        plan.level = str(data.get("level", "") or "").strip()
        plan.nature = str(data.get("nature", "") or "").strip()
        plan.core_contradiction = str(data.get("core_contradiction", "") or "").strip()
        plan.propositions = [
            _strip_dimension_label(p)
            for p in _as_str_list(data.get("propositions"),
                                  limit=int(cfg.get("max_propositions")))
        ]
        plan.keywords = _as_str_list(data.get("keywords"), limit=10)
        plan.missing_perspective = str(data.get("missing_perspective", "") or "").strip()

        # 至少要产出一个可用于检索的信号，否则视为分析失败
        plan.analysis_ok = bool(plan.propositions or plan.keywords)

        if plan.analysis_ok:
            logger.info(
                f"前置分析完成: 域={plan.domain or '?'} 层={plan.level or '?'} "
                f"性质={plan.nature or '?'} | 命题 {len(plan.propositions)} 条 "
                f"| 范畴 {len(plan.keywords)} 个"
            )
            if plan.core_contradiction:
                logger.info(f"  核心矛盾: {plan.core_contradiction[:60]}")
        else:
            logger.warning("前置分析未产出检索信号，降级为原问题检索")

    except Exception as e:
        logger.warning(f"前置分析失败，降级为原问题检索: {e}")

    return plan


# ======================================================================
# 原文阅读器：模糊搜索的关键词抽取
# ======================================================================

# 用户在阅读器里往往是"我记得有一段讲……"这种描述，而不是精确原句。
# 这里只做一件事：把白话描述转成经典文献里真实会出现的术语，
# 不解构问题、不生成回答，因此比 build_query_plan 轻得多。
_KEYWORD_PROMPT = """你是马克思主义经典文献的检索助手。

用户想在经典著作原文中找到某一段话，但只能给出模糊的描述。
你的任务是把这段描述转换为便于检索的检索词。

用户描述：{query}

要求：
1. keywords：{n} 个以内的经典文献术语，用于关键词匹配。
   必须是马克思、恩格斯、列宁、斯大林、毛泽东著作中真实使用的表述，
   不要用现代白话或学术黑话。
2. proposition：一句话，模仿经典著作的行文口吻，
   写出用户可能在找的那句话的大致样子。用于语义相似度匹配。

只输出 JSON，不要任何其他文字：
{"keywords": ["...", "..."], "proposition": "..."}"""


def extract_search_keywords(sdk_client, model_name: str, query: str,
                            max_keywords: int = 5,
                            timeout: Optional[float] = None) -> dict:
    """把用户的模糊描述转成检索词

    返回 {"keywords": [...], "proposition": str, "llm_ok": bool}。

    与 build_query_plan 一样，任何异常都不上抛：LLM 不可用时
    退回"直接拿原始描述去检索"，效果差一些但功能不中断。
    """
    cfg = get_config()
    fallback = {"keywords": [], "proposition": "", "llm_ok": False}

    if not query or not query.strip():
        return fallback

    try:
        filled = _KEYWORD_PROMPT.replace("{query}", query.strip()) \
                                .replace("{n}", str(max_keywords))

        req = {
            "model": model_name,
            "messages": [{"role": "user", "content": filled}],
            "temperature": cfg.get("planner_temperature"),
            # 最终 JSON 只有 100 多字符，但 completion_tokens 实测能到 586：
            # 模型的思考过程也计入该额度。给少了会 finish_reason=length
            # 且 content 为空串，整次抽取作废。按实测峰值留一倍余量。
            "max_tokens": 1500,
            "timeout": float(timeout if timeout is not None else cfg.get("planner_timeout")),
        }

        try:
            resp = sdk_client.chat.completions.create(
                **req, response_format={"type": "json_object"}
            )
        except Exception:
            resp = sdk_client.chat.completions.create(**req)

        data = _extract_json(resp.choices[0].message.content or "")
        if data is None:
            # 区分"被截断"和"格式不对"：前者调大 max_tokens 就能解决，
            # 混在一起报会让人查错方向
            if resp.choices[0].finish_reason == "length":
                logger.warning("关键词抽取输出被截断（max_tokens 不足），降级为原文检索")
            else:
                logger.warning("关键词抽取未返回可解析 JSON，降级为原文检索")
            return fallback

        keywords = _as_str_list(data.get("keywords"), limit=max_keywords)
        proposition = str(data.get("proposition", "") or "").strip()

        if not keywords and not proposition:
            return fallback

        logger.info(f"关键词抽取: {'、'.join(keywords) or '(无)'}")
        return {"keywords": keywords, "proposition": proposition, "llm_ok": True}

    except Exception as e:
        logger.warning(f"关键词抽取失败，降级为原文检索: {e}")
        return fallback
