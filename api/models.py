from pydantic import BaseModel
from typing import List, Optional


class SourceItem(BaseModel):
    """来源文档条目"""
    title: str = ""
    author: str = ""
    score: float = 0.0
    excerpt: str = ""
    source_url: str = ""


class ChatRequest(BaseModel):
    """聊天请求"""
    question: str
    conversation_id: Optional[str] = None
    mode: str = "general"  # general / methodology / original
    model: str = "deepseek-chat"
    thinking_mode: bool = False
    search_mode: bool = False


class ChatResponse(BaseModel):
    """聊天响应"""
    answer: str
    sources: List[SourceItem] = []
    conversation_id: str = ""


class Message(BaseModel):
    """单条消息"""
    role: str  # user / assistant
    content: str
    sources: Optional[List[SourceItem]] = None


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


class ModelOption(BaseModel):
    """模型选项"""
    id: str
    name: str
    provider: str


class ModelUpdateRequest(BaseModel):
    """更新模型请求"""
    model: str


# ── 设置相关模型 ─────────────────────────────────

class SettingsItem(BaseModel):
    """设置项"""
    key: str
    label: str = ""
    type: str = "text"  # text / number / select / boolean / password
    value: str = ""
    options: Optional[List[str]] = None
    description: str = ""
    category: str = "general"  # general / search / api / about


class SettingsResponse(BaseModel):
    """设置响应"""
    settings: List[SettingsItem]


class SettingsUpdateRequest(BaseModel):
    """更新设置请求"""
    updates: dict


class CacheClearRequest(BaseModel):
    """清除缓存请求"""
    cache_type: str = "all"  # all / answer / embedding


class StatsResponse(BaseModel):
    """知识库统计"""
    document_count: int = 0
    vector_count: int = 0
    source_files: int = 0
    cache_embeddings: int = 0
    cache_answers: int = 0
