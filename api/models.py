from pydantic import BaseModel
from typing import List, Optional


class SourceItem(BaseModel):
    """来源文档条目"""
    title: str = ""
    author: str = ""
    score: float = 0.0
    excerpt: str = ""
    source_url: str = ""
    doc_uuid: str = ""      # 片段 uuid，阅读器据此定位到原文
    source_file: str = ""   # 原始 Markdown 文件名


class ChatRequest(BaseModel):
    """聊天请求

    parent_message_id 决定这轮提问挂在消息树的哪个节点下：
      - 不传：接在当前激活分支的末尾（正常追问）
      - 传某条用户消息的 parent_id：在同一层新建一个版本（修改提问后重发）
    regenerate_of 用于「重新生成」，值为要重做的那条助手消息 id。
    """
    question: str
    conversation_id: Optional[str] = None
    mode: str = "general"  # general / methodology / original
    model: str = "deepseek-chat"
    thinking_effort: Optional[str] = None  # off / high / max（参考 DSH 推理等级）
    thinking_mode: bool = False            # 旧字段：True 等价 thinking_effort="high"
    search_mode: bool = False
    parent_message_id: Optional[int] = None
    regenerate_of: Optional[int] = None


class Message(BaseModel):
    """单条消息

    variant_count / variant_index 供前端渲染「< 2/2 >」版本切换器：
    同一个 parent 下有多少个版本、当前显示的是第几个（从 0 起）。
    variant_count == 1 时前端不必显示切换器。

    thinking_content / stage_detail 是"会话的一部分"而不是浏览器
    临时数据：思考过程与问题解构卡片随消息落库，刷新页面 / 切换
    版本 / 重新生成后都能完整还原，不再依赖 localStorage。
    """
    id: Optional[int] = None
    role: str  # user / assistant
    content: str
    sources: Optional[List[SourceItem]] = None
    parent_id: Optional[int] = None
    variant_count: int = 1
    variant_index: int = 0
    thinking_content: str = ""           # 思考过程（仅思考档非空）
    stage_detail: Optional[dict] = None  # 问题解构卡片数据（检索前置分析）


class MessageVariants(BaseModel):
    """某条消息的全部同级版本"""
    message_id: int
    parent_id: Optional[int] = None
    variant_index: int = 0
    variants: List[Message] = []


class SwitchVariantRequest(BaseModel):
    """切换到指定版本

    传要切过去的那条消息 id，后端据此重算激活分支。
    """
    message_id: int


class Conversation(BaseModel):
    """对话列表中的摘要"""
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0


class ConversationDetail(BaseModel):
    """对话详情（含消息列表）"""
    id: str
    title: str
    messages: List[Message] = []
    created_at: str
    updated_at: str


class ConversationRenameRequest(BaseModel):
    """重命名对话请求"""
    title: str


class ModelOption(BaseModel):
    """模型选项"""
    id: str
    name: str
    provider: str


class ModelUpdateRequest(BaseModel):
    """更新模型请求"""
    model: str


class ModelAddRequest(BaseModel):
    """添加模型请求(写入 OPENAI_MODEL_LIST)"""
    id: str
    name: str = ""   # 留空则显示名取模型 ID


def parse_model_list(raw: str) -> list:
    """解析 OPENAI_MODEL_LIST 字符串为 [{id, name}]

    格式: "id1:显示名1,id2:显示名2";无冒号时显示名取 ID。
    纯函数,无副作用,供路由与测试共用。
    """
    items = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            mid, name = part.split(":", 1)
            mid, name = mid.strip(), name.strip()
        else:
            mid, name = part, part
        items.append({"id": mid, "name": name})
    return items


def serialize_model_list(items: list) -> str:
    """把 [{id, name}] 序列化为 "id1:name1,id2:name2"

    显示名同 ID 时省略冒号部分;空 ID 条目丢弃。
    """
    parts = []
    for it in items:
        mid = (it.get("id") or "").strip()
        name = (it.get("name") or "").strip()
        if not mid:
            continue
        parts.append(mid if name == mid or not name else f"{mid}:{name}")
    return ",".join(parts)


# ── 设置相关模型 ─────────────────────────────────
# 设置项的结构由 rag/config_store.py 的 CONFIG_SCHEMA 定义，
# GET /api/settings 直接返回其生成的 schema，故此处不再重复定义响应模型。

class SettingsUpdateRequest(BaseModel):
    """更新设置请求"""
    updates: dict


class SettingsResetRequest(BaseModel):
    """恢复默认设置请求；key 为空表示重置全部"""
    key: Optional[str] = None


class CacheClearRequest(BaseModel):
    """清除缓存请求"""
    cache_type: str = "all"  # all / answer / embedding


class ApiTestRequest(BaseModel):
    """API 连通性测试请求

    各字段留空表示使用当前已保存的配置——因此用户可以
    直接测"已保存的值",也可以填新值先测后保存。
    """
    target: str = "chat"       # chat / embed / rerank
    api_base_url: str = ""
    api_key: str = ""
    model: str = ""


class ReaderSearchRequest(BaseModel):
    """原文模糊搜索请求

    scope="current" 时只在 source 指定的那本书里找，需同时传 source。
    """
    query: str
    scope: str = "all"                 # all / current
    source: Optional[str] = None


class StatsResponse(BaseModel):
    """知识库统计"""
    document_count: int = 0
    vector_count: int = 0
    source_files: int = 0
    cache_embeddings: int = 0
    cache_answers: int = 0
    kb_version: str = "legacy"   # 当前知识库版本号；legacy 表示传统 rag/ 索引
    perf: Optional[dict] = None  # 最近请求的平均耗时汇总（毫秒）
