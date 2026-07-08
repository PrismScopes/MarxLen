import os
import asyncio
import json
import logging
import threading
import portalocker
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

from .models import (
    ChatRequest, ChatResponse, SourceItem,
    Conversation, ConversationDetail,
    ModelOption, ModelUpdateRequest,
    SettingsResponse, SettingsItem,
    SettingsUpdateRequest, CacheClearRequest,
    StatsResponse,
)
from .conversation_store import ConversationStore
from .settings_store import SettingsStore

# 加载 .env，用于读取模型配置
load_dotenv(override=True)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")
conv_store = ConversationStore()
settings_store = SettingsStore()

# ── 全局 RAG 引擎引用（在 main.py 中初始化后注入）──
rag_pipeline = None


def set_rag_pipeline(pipeline):
    global rag_pipeline
    rag_pipeline = pipeline


# ================================================================
# 1. POST /api/chat - 发送消息并获取回答（SSE 流式输出）
# ================================================================

@router.post("/chat")
async def chat(request: ChatRequest):
    """发送消息并流式获取回答"""
    global rag_pipeline
    if rag_pipeline is None:
        raise HTTPException(status_code=503, detail="RAG 引擎尚未初始化")

    # 1. 对话管理
    conv_id = request.conversation_id
    if not conv_id:
        # 新对话：使用问题前 20 字作为标题
        title = request.question[:20] + ("..." if len(request.question) > 20 else "")
        conv = conv_store.create_conversation(title=title)
        conv_id = conv.id
    else:
        conv_detail = conv_store.get_conversation(conv_id)
        if conv_detail is None:
            raise HTTPException(status_code=404, detail="对话不存在")

    # 2. 保存用户消息
    conv_store.add_message(conv_id, "user", request.question)

    # 3. 获取对话历史用于上下文
    history = []
    conv_detail = conv_store.get_conversation(conv_id)
    if conv_detail and conv_detail.messages:
        for msg in conv_detail.messages[:-1]:  # 排除刚插入的用户消息
            history.append({
                "role": msg.role,
                "content": msg.content[:500],  # 取前 500 字避免超长
            })
        logging.info(f"  上下文: {len(history)} 条历史消息")

    async def event_generator():
        """SSE 事件生成器"""
        try:
            full_answer = ""
            thinking_content = ""
            sources_data = []

            # 调用 RAG 流式接口（在线程中运行同步生成器，避免阻塞事件循环）
            fetch_k = int(settings_store.get("fetch_k", 30))
            top_k = int(settings_store.get("top_k", 8))

            loop = asyncio.get_running_loop()
            queue = asyncio.Queue(maxsize=128)

            def run_generator():
                try:
                    for item in rag_pipeline.ask_stream(
                        question=request.question,
                        top_k=top_k,
                        fetch_k=fetch_k,
                        thinking_mode=request.thinking_mode,
                        history=history,
                        search_mode=request.search_mode,
                    ):
                        fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                        try:
                            fut.result(timeout=5)  # 5秒超时防止死锁
                        except Exception:
                            break  # 超时或取消，终止生成
                except Exception as e:
                    asyncio.run_coroutine_threadsafe(
                        queue.put({"type": "error", "detail": str(e)}), loop
                    )

            thread = threading.Thread(target=run_generator, daemon=True)
            thread.start()

            # 异步从队列读取（客户端断开时自动退出）
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=60)
                    except asyncio.TimeoutError:
                        break  # 超时退出，不再等待
                    if item["type"] == "done":
                        sources_data = item.get("sources", [])
                        search_refs = item.get("search_refs", [])
                        break
                    elif item["type"] == "error":
                        raise Exception(item.get("detail", "未知错误"))
                    elif item["type"] == "thinking":
                        content = item["content"]
                        thinking_content += content
                        yield {
                            "event": "thinking",
                            "data": json.dumps({"content": content}, ensure_ascii=False),
                        }
                    elif item["type"] == "token":
                        content = item["content"]
                        full_answer += content
                        yield {
                            "event": "token",
                            "data": json.dumps({"content": content}, ensure_ascii=False),
                        }
            except asyncio.CancelledError:
                # 客户端断开，停止读取
                return

            # 保存助手回答
            source_items = []
            for s in sources_data:
                source_items.append(SourceItem(
                    title=s.get("title", ""),
                    author=s.get("author", ""),
                    score=float(s.get("score", 0)),
                    excerpt=s.get("excerpt", ""),
                    source_url=s.get("source_url", ""),
                ))
            conv_store.add_message(conv_id, "assistant", full_answer, sources=source_items)

            # 发送完成事件（含来源、联网搜索结果、conversation_id）
            yield {
                "event": "done",
                "data": json.dumps({
                    "conversation_id": conv_id,
                    "sources": [s.model_dump() for s in source_items],
                    "references": search_refs,
                    "thinking_content": thinking_content if request.thinking_mode else "",
                }, ensure_ascii=False),
            }

        except Exception as e:
            logger.exception("RAG 流式调用失败")
            yield {
                "event": "error",
                "data": json.dumps({"detail": str(e)}, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator(), headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


# ================================================================
# 2. GET /api/conversations - 获取对话历史列表
# ================================================================

@router.get("/conversations", response_model=list[Conversation])
async def list_conversations(limit: int = 50):
    return conv_store.get_conversations(limit=limit)


# ================================================================
# 3. GET /api/conversations/{id} - 获取对话详情
# ================================================================

@router.get("/conversations/{conv_id}", response_model=ConversationDetail)
async def get_conversation(conv_id: str):
    detail = conv_store.get_conversation(conv_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return detail


# ================================================================
# 4. DELETE /api/conversations/{id} - 删除对话
# ================================================================

@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    detail = conv_store.get_conversation(conv_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    conv_store.delete_conversation(conv_id)
    return {"ok": True}


# ================================================================
# 5. GET /api/models - 获取可用模型列表
# ================================================================

@router.get("/models", response_model=list[ModelOption])
async def list_models():
    """从 .env 配置中读取可用模型列表
    
    通过 DEEPSEEK_MODEL_LIST 配置，格式：id1:显示名1,id2:显示名2
    示例：DEEPSEEK_MODEL_LIST=deepseek-chat:DeepSeek-V3,deepseek-reasoner:DeepSeek-R1
    不配置时默认显示 DEEPSEEK_MODEL 指定模型的单选项。
    """
    model_list_raw = os.getenv("DEEPSEEK_MODEL_LIST", "")
    if model_list_raw:
        models = []
        for item in model_list_raw.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                mid, name = item.split(":", 1)
                mid, name = mid.strip(), name.strip()
            else:
                mid, name = item, item
            models.append(ModelOption(id=mid, name=name, provider="DeepSeek"))
        if models:
            return models

    # 没有任何列表配置，显示 DEEPSEEK_MODEL 指定模型
    configured_model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    return [ModelOption(id=configured_model, name=configured_model, provider="DeepSeek")]


# ================================================================
# 6. POST /api/settings/model - 修改当前模型
# ================================================================

@router.post("/settings/model")
async def update_model(req: ModelUpdateRequest):
    """更新当前使用的模型（通过更新 .env 文件中的 DEEPSEEK_MODEL）"""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rag", ".env")
    try:
        with portalocker.Lock(env_path, mode='r', encoding='utf-8', flags=portalocker.LOCK_EX) as f:
            lines = f.readlines()
        with portalocker.Lock(env_path, mode='w', encoding='utf-8', flags=portalocker.LOCK_EX) as f:
            for line in lines:
                if line.strip().startswith("DEEPSEEK_MODEL="):
                    f.write(f"DEEPSEEK_MODEL={req.model}\n")
                else:
                    f.write(line)
        # 重新加载环境变量
        load_dotenv(override=True)
        logger.info(f"模型已更新为: {req.model}")
        return {"ok": True, "model": req.model}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新模型失败: {str(e)}")


# ================================================================
# 7. GET /api/health - 健康检查
# ================================================================

@router.get("/health")
async def health_check():
    return {"status": "ok", "rag_initialized": rag_pipeline is not None}


# ================================================================
# 8. GET /api/settings - 获取所有设置
# ================================================================

@router.get("/settings", response_model=SettingsResponse)
async def get_settings():
    """获取所有可配置的设置项"""
    raw = settings_store.get_all()

    settings_list = [
        SettingsItem(
            key="model",
            label="当前模型",
            type="select",
            value=raw.get("model", ""),
            description="用于生成回答的大语言模型",
            category="general",
        ),
        SettingsItem(
            key="api_base_url",
            label="API 地址",
            type="text",
            value=raw.get("api_base_url", ""),
            description="DeepSeek API 端点地址",
            category="api",
        ),
        SettingsItem(
            key="embed_api_base_url",
            label="嵌入 API 地址",
            type="text",
            value=raw.get("embed_api_base_url", ""),
            description="Embedding 和 Rerank 的 API 端点",
            category="api",
        ),
        SettingsItem(
            key="temperature",
            label="温度参数",
            type="number",
            value=str(raw.get("temperature", 0.3)),
            description="控制回答的创造性（0=精确，1=多样）",
            category="general",
        ),
        SettingsItem(
            key="top_k",
            label="返回结果数",
            type="number",
            value=str(raw.get("top_k", 8)),
            description="每次回答展示几个参考来源",
            category="search",
        ),
        SettingsItem(
            key="fetch_k",
            label="候选数量",
            type="number",
            value=str(raw.get("fetch_k", 30)),
            description="重排序前从检索池取多少候选",
            category="search",
        ),
        SettingsItem(
            key="enable_reranker",
            label="启用重排序",
            type="boolean",
            value=str(raw.get("enable_reranker", True)).lower(),
            description="关闭可节省一次 API 调用（用 RRF 排序代替）",
            category="search",
        ),
        SettingsItem(
            key="default_mode",
            label="默认模式",
            type="select",
            value=raw.get("default_mode", "general"),
            options=["general", "methodology", "original"],
            description="启动时默认的问答模式",
            category="general",
        ),
    ]
    return SettingsResponse(settings=settings_list)


# ================================================================
# 9. PUT /api/settings - 更新设置
# ================================================================

@router.put("/settings")
async def update_settings(req: SettingsUpdateRequest):
    """更新设置"""
    try:
        result = settings_store.update(req.updates)
        # 同步运行时设置到 RAG 实例
        if rag_pipeline is not None:
            if "temperature" in req.updates:
                rag_pipeline.llm.temperature = float(req.updates["temperature"])
            if "top_k" in req.updates:
                rag_pipeline.retriever.top_k = int(req.updates["top_k"])
            if "fetch_k" in req.updates:
                rag_pipeline.retriever.fetch_k = int(req.updates["fetch_k"])
        return {"ok": True, "settings": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新设置失败: {e}")


# ================================================================
# 10. POST /api/settings/cache/clear - 清除缓存
# ================================================================

@router.post("/settings/cache/clear")
async def clear_cache(req: CacheClearRequest):
    """清除嵌入或回答缓存"""
    result = settings_store.clear_cache(
        cache_type=req.cache_type,
        rag_pipeline=rag_pipeline,
    )
    return {"ok": True, **result}


# ================================================================
# 11. GET /api/settings/stats - 知识库统计
# ================================================================

@router.get("/settings/stats", response_model=StatsResponse)
async def get_stats():
    """获取知识库统计信息"""
    return settings_store.get_stats(rag_pipeline=rag_pipeline)
