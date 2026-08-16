import os
import time
import uuid
import asyncio
import json
import logging
import threading
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

from .models import (
    ChatRequest, ChatResponse, SourceItem,
    Conversation, ConversationDetail, ConversationRenameRequest,
    MessageVariants, SwitchVariantRequest,
    ModelOption, ModelUpdateRequest,
    SettingsUpdateRequest, SettingsResetRequest, CacheClearRequest,
    ApiTestRequest, StatsResponse,
)
from .conversation_store import ConversationStore
from .settings_store import SettingsStore
from rag.config_store import get_config, CONFIG_SCHEMA

# 加载 .env，用于读取模型配置
load_dotenv(override=True)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")
conv_store = ConversationStore()
settings_store = SettingsStore()
config = get_config()

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

    # 请求关联 ID:贯穿本请求全部日志,SSE 首帧带给前端
    request_id = uuid.uuid4().hex[:12]
    # 思考强度:优先新字段 thinking_effort;旧 thinking_mode=True 等价 high
    effort = request.thinking_effort
    if effort not in ("off", "high", "max"):
        effort = "high" if request.thinking_mode else "off"
    logger.info("收到提问 [question=%.40s...] [mode=%s effort=%s search=%s]",
                request.question, request.mode, effort,
                request.search_mode)

    # 1. 对话管理
    conv_id = request.conversation_id
    if not conv_id:
        # 新对话：取问题开头若干字作为标题
        title_len = int(config.get("title_len"))
        title = request.question[:title_len] + ("..." if len(request.question) > title_len else "")
        conv = conv_store.create_conversation(title=title)
        conv_id = conv.id
    else:
        conv_detail = conv_store.get_conversation(conv_id)
        if conv_detail is None:
            raise HTTPException(status_code=404, detail="对话不存在")

    # 2. 保存用户消息
    #
    # 改提问和重新生成都不能覆盖旧内容——旧内容是同一问题的另一个版本，
    # 用户要能切回去看。所以这里不是追加，而是在同一个父节点下新开一个分支。
    parent_id = request.parent_message_id
    branch = False

    if request.regenerate_of is not None:
        # 重新生成：目标是某条助手消息，新回答要和它做兄弟，
        # 即挂到它的父节点（也就是触发它的那条用户消息）下
        target = conv_store.get_message(conv_id, request.regenerate_of)
        if target is None:
            raise HTTPException(status_code=404, detail="要重新生成的消息不存在")
        if target[1] != "assistant":
            raise HTTPException(status_code=400, detail="只能对助手回答重新生成")
        # 用户消息不重复写入，直接把回答挂到原来那条提问下
        user_msg_id = target[5]
        branch = True
    else:
        if parent_id is not None:
            # 修改提问后重发：parent_message_id 指向被改那条消息的父节点，
            # 新提问与旧提问成为兄弟版本
            if parent_id == 0:
                # 0 是前端表达"根层"的约定值，根消息的 parent 为 NULL
                parent_id = None
            elif conv_store.get_message(conv_id, parent_id) is None:
                raise HTTPException(status_code=404, detail="父消息不存在")
            branch = True

        user_msg = conv_store.add_message(
            conv_id, "user", request.question,
            parent_id=parent_id, branch=branch,
        )
        user_msg_id = user_msg.id
        branch = False  # 助手回答接在这条新用户消息之后，不再分支

    # 3. 获取对话历史用于上下文
    history = []
    msg_len = int(config.get("history_msg_len"))
    conv_detail = conv_store.get_conversation(conv_id)
    if conv_detail and conv_detail.messages:
        for msg in conv_detail.messages[:-1]:  # 排除刚插入的用户消息
            history.append({
                "role": msg.role,
                "content": msg.content[:msg_len],  # 截断避免超长
            })
        logging.info(f"  上下文: {len(history)} 条历史消息")

    async def event_generator():
        """SSE 事件生成器"""
        try:
            from rag.telemetry import set_request_id, perf_recorder
            set_request_id(request_id)   # 协程侧日志带 request_id
            full_answer = ""
            thinking_content = ""
            sources_data = []
            search_refs = []
            thinking_done_sent = False
            done_timings = None
            done_ref_report = None

            # 首帧即带回 request_id:前端可据此把日志与本次会话对应起来
            yield {
                "event": "meta",
                "data": json.dumps({"request_id": request_id}, ensure_ascii=False),
            }

            # 调用 RAG 流式接口（在线程中运行同步生成器，避免阻塞事件循环）
            fetch_k = int(config.get("fetch_k"))
            top_k = int(config.get("top_k"))

            loop = asyncio.get_running_loop()
            queue = asyncio.Queue(maxsize=int(config.get("stream_queue_size")))

            def run_generator():
                from rag.telemetry import set_request_id as _set_rid
                _set_rid(request_id)     # 生成线程侧日志带 request_id
                try:
                    for item in rag_pipeline.ask_stream(
                        question=request.question,
                        top_k=top_k,
                        fetch_k=fetch_k,
                        thinking_effort=effort,
                        history=history,
                        search_mode=request.search_mode,
                        request_id=request_id,
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
                        item = await asyncio.wait_for(
                            queue.get(), timeout=float(config.get("stream_timeout"))
                        )
                    except asyncio.TimeoutError:
                        break  # 超时退出，不再等待
                    if item["type"] == "done":
                        sources_data = item.get("sources", [])
                        search_refs = item.get("search_refs", [])
                        done_timings = item.get("timings") or {}
                        done_ref_report = item.get("ref_report") or {}
                        break
                    elif item["type"] == "error":
                        raise Exception(item.get("detail", "未知错误"))
                    elif item["type"] == "stage":
                        # 检索各阶段的进度，前端据此展示"正在做什么"，
                        # 填补提问到首个 token 之间的等待
                        yield {
                            "event": "stage",
                            "data": json.dumps({
                                "stage": item.get("stage", ""),
                                "status": item.get("status", "running"),
                                "text": item.get("text", ""),
                                "detail": item.get("detail"),
                                "elapsed_ms": item.get("elapsed_ms"),
                            }, ensure_ascii=False),
                        }
                    elif item["type"] == "search_result":
                        # 联网搜索结果先于正文送达，前端据此渲染参考链接面板
                        search_refs = item.get("references", [])
                        yield {
                            "event": "search_result",
                            "data": json.dumps({"references": search_refs}, ensure_ascii=False),
                        }
                    elif item["type"] == "thinking":
                        content = item["content"]
                        thinking_content += content
                        yield {
                            "event": "thinking",
                            "data": json.dumps({"content": content}, ensure_ascii=False),
                        }
                    elif item["type"] == "token":
                        content = item["content"]
                        # 首个正文 token 意味着思考阶段结束，通知前端收起思考面板
                        if thinking_content and not thinking_done_sent:
                            thinking_done_sent = True
                            yield {"event": "thinking_done", "data": "{}"}
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
                    # 这两个字段是原文阅读器定位的依据，缺了来源卡片就跳不过去
                    doc_uuid=s.get("doc_uuid", ""),
                    source_file=s.get("source_file", ""),
                ))
            # 显式挂到本轮那条用户消息下：重新生成时激活叶子可能已被
            # 别的请求改动，靠"接在末尾"会挂错位置
            assistant_msg = conv_store.add_message(
                conv_id, "assistant", full_answer, sources=source_items,
                parent_id=user_msg_id, branch=branch,
            )

            # 发送完成事件（含来源、联网搜索结果、conversation_id）
            # message_id / variant_* 让前端立刻能渲染版本切换器，
            # 不必再多请求一次对话详情。
            # timings 为本次请求各阶段耗时（总耗时/解构/检索/首 token/生成），
            # ref_report 为引用后处理的覆盖统计。
            try:
                from rag.telemetry import perf_recorder
                if done_timings:
                    perf_recorder.record(done_timings)
            except Exception:
                pass
            logger.info("请求完成 [total=%.0fms analyze=%.0fms retrieve=%.0fms "
                        "first_token=%.0fms generate=%.0fms] [字数=%d]",
                        done_timings.get("total_ms", 0),
                        done_timings.get("analyze_ms", 0),
                        done_timings.get("retrieve_ms", 0),
                        done_timings.get("first_token_ms", 0),
                        done_timings.get("generate_ms", 0),
                        len(full_answer))

            yield {
                "event": "done",
                "data": json.dumps({
                    "conversation_id": conv_id,
                    "sources": [s.model_dump() for s in source_items],
                    "references": search_refs,
                    "thinking_content": thinking_content if effort != "off" else "",
                    "message_id": assistant_msg.id,
                    "user_message_id": user_msg_id,
                    "variant_count": assistant_msg.variant_count,
                    "variant_index": assistant_msg.variant_index,
                    "request_id": request_id,
                    "timings": done_timings,
                    "ref_report": done_ref_report,
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
async def list_conversations(limit: Optional[int] = None):
    """不传 limit 时使用设置项 conversation_list_limit"""
    if limit is None:
        limit = int(config.get("conversation_list_limit"))
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
# 3.2 GET /api/conversations/{id}/messages/{mid}/variants - 取同级版本
# ================================================================

@router.get("/conversations/{conv_id}/messages/{msg_id}/variants",
            response_model=MessageVariants)
async def get_message_variants(conv_id: str, msg_id: int):
    """列出某条消息的全部版本

    前端渲染「< 2/2 >」时，若只需要知道有几个版本，对话详情里的
    variant_count 就够了；需要预览各版本内容时才调这个接口。
    """
    if conv_store.get_conversation(conv_id) is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    result = conv_store.get_variants(conv_id, msg_id)
    if result is None:
        raise HTTPException(status_code=404, detail="消息不存在")
    return result


# ================================================================
# 3.3 POST /api/conversations/{id}/switch - 切换到指定版本
# ================================================================

@router.post("/conversations/{conv_id}/switch", response_model=ConversationDetail)
async def switch_variant(conv_id: str, request: SwitchVariantRequest):
    """把激活分支切到经过 message_id 的那一条

    返回切换后的完整对话详情，前端直接整体重渲染即可，
    不需要自己推算哪些消息该换掉。
    """
    if conv_store.get_conversation(conv_id) is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    detail = conv_store.switch_variant(conv_id, request.message_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="消息不存在")
    return detail


# ================================================================
# 3.5 PATCH /api/conversations/{id} - 重命名对话
# ================================================================

@router.patch("/conversations/{conv_id}", response_model=Conversation)
async def rename_conversation(conv_id: str, request: ConversationRenameRequest):
    """修改对话标题。前端右键菜单的"重命名"走这个接口。"""
    detail = conv_store.get_conversation(conv_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="标题不能为空")
    # 限制长度，避免侧边栏被超长标题撑破
    title = title[:100]

    conv_store.update_title(conv_id, title)
    for conv in conv_store.get_conversations(limit=200):
        if conv.id == conv_id:
            return conv
    raise HTTPException(status_code=404, detail="对话不存在")


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
    """读取可用模型列表

    候选项来自设置项 model_list（落库为 .env 的 OPENAI_MODEL_LIST），
    格式：id1:显示名1,id2:显示名2
    留空时只返回当前正在使用的模型这一项。
    """
    model_list_raw = config.get("model_list") or ""
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

    # 没有配置候选列表，只显示当前模型
    configured_model = config.get("model")
    return [ModelOption(id=configured_model, name=configured_model, provider="DeepSeek")]


# ================================================================
# 6. POST /api/settings/model - 修改当前模型
# ================================================================

@router.post("/settings/model")
async def update_model(req: ModelUpdateRequest):
    """更新当前使用的模型（写入 rag/.env 的 OPENAI_MODEL）

    等价于 PUT /api/settings {"updates": {"model": ...}}，
    保留此端点是为了兼容前端已有的模型切换入口。
    """
    try:
        config.update({"model": req.model})
        _apply_runtime_settings({"model": req.model})
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
# 8. GET /api/settings - 获取所有设置（含前端渲染元数据）
# ================================================================

@router.get("/settings")
async def get_settings():
    """返回完整的设置描述，供前端自动渲染设置页面

    响应结构：
      categories: [{id, label, icon, description}]  分组，顺序即显示顺序
      items:      [{key, label, type, value, default, category, description,
                    options, min, max, step, unit, secret, is_set,
                    advanced, requires_restart, is_default}]

    前端无需硬编码任何字段，遍历 items 按 type 渲染对应控件即可。
    新增配置项只需在 rag/config_store.py 的 CONFIG_SCHEMA 追加一条。
    """
    return config.get_schema()


# ================================================================
# 9. PUT /api/settings - 更新设置
# ================================================================

@router.put("/settings")
async def update_settings(req: SettingsUpdateRequest):
    """更新设置

    非法值会被自动钳制到合法区间或回退默认值，不会报错。
    未知的 key 会被忽略。敏感项（API Key）传空字符串表示不修改。
    """
    try:
        settings = config.update(req.updates)
        _apply_runtime_settings(req.updates)

        # 告知前端哪些改动需要重启才能生效
        restart_keys = [
            k for k in req.updates
            if any(it.key == k and it.requires_restart for it in CONFIG_SCHEMA)
        ]
        return {
            "ok": True,
            "settings": settings,
            "restart_required": restart_keys,
        }
    except Exception as e:
        logger.exception("更新设置失败")
        raise HTTPException(status_code=500, detail=f"更新设置失败: {e}")


# ================================================================
# 9b. POST /api/settings/reset - 恢复默认设置
# ================================================================

@router.post("/settings/reset")
async def reset_settings(req: SettingsResetRequest):
    """恢复默认值。不传 key 则重置全部。

    API 地址与密钥（存于 .env）不参与重置，避免误删用户凭据。
    """
    try:
        settings = config.reset(req.key)
        _apply_runtime_settings(settings)
        return {"ok": True, "settings": settings}
    except Exception as e:
        logger.exception("重置设置失败")
        raise HTTPException(status_code=500, detail=f"重置设置失败: {e}")


def _apply_runtime_settings(updates: dict):
    """把设置同步到已实例化的对象上，使其无需重启即生效

    只有那些在初始化时被读取、之后不再重新读取的值才需要在此同步。
    top_k / fetch_k 等在每次请求时都会重新读配置，无需处理。

    密钥与端点变化时重建客户端（reconfigure），实现"保存即生效"，
    与 DSH 的配置热更新体验一致；embed_model 例外——换嵌入模型
    会改变向量空间，必须重建索引，仍需重启。
    """
    if rag_pipeline is None:
        return
    try:
        if "temperature" in updates:
            rag_pipeline.llm.temperature = config.get("temperature")
        if "max_tokens" in updates:
            mt = config.get("max_tokens")
            # 0 表示不限制，交由模型自行决定
            rag_pipeline.llm.max_tokens = mt if mt > 0 else None
        if "top_p" in updates:
            rag_pipeline.llm.top_p = config.get("top_p")
        if "model" in updates:
            new_model = config.get("model")
            rag_pipeline.model_name = new_model
            rag_pipeline.llm.model_name = new_model
        # 端点/密钥/重排模型:重建客户端,保存即生效
        if any(k in updates for k in (
                "api_key", "api_base_url", "embed_api_key",
                "embed_api_base_url", "rerank_model",
                "embed_timeout", "embed_max_retries")):
            rag_pipeline.reconfigure()
        if "reader_corpus_dir" in updates:
            # 换了语料目录，旧正文缓存全部失效
            from api.reader import clear_cache as clear_reader_cache
            clear_reader_cache()
    except Exception as e:
        # 同步失败不应让整个请求失败，配置本身已保存成功
        logger.warning(f"运行时设置同步失败（重启后仍会生效）: {e}")


# ================================================================
# 9c. POST /api/settings/test-api - API 连通性测试
# ================================================================

# 错误分类:把 openai 库的异常翻译成用户能看懂的一句话
def _classify_api_error(e: Exception) -> str:
    try:
        from openai import (
            AuthenticationError, NotFoundError, APITimeoutError,
            APIConnectionError, PermissionDeniedError, RateLimitError,
        )
        if isinstance(e, AuthenticationError):
            return "密钥无效(401),请检查 API Key"
        if isinstance(e, PermissionDeniedError):
            return "无权限(403),该密钥可能没有访问此模型的权限"
        if isinstance(e, RateLimitError):
            return "触发限流(429),稍后重试或检查额度"
        if isinstance(e, NotFoundError):
            return "端点或资源不存在(404),请检查 API 地址与模型名"
        if isinstance(e, APITimeoutError):
            return "请求超时,请检查网络或换更快的服务商"
        if isinstance(e, APIConnectionError):
            return "无法连接,请检查 API 地址与网络"
    except ImportError:
        pass
    return str(e)[:200]


@router.post("/settings/test-api")
async def test_api(req: ApiTestRequest):
    """测试 API 连通性(可用未保存的输入值,先测后存)

    target:
      chat   - 拉取模型列表,验证密钥与端点(不产生生成费用)
      embed  - 嵌入一小段文本,返回向量维度
      rerank - 重排两条文档,验证重排服务
    """
    if req.target not in ("chat", "embed", "rerank"):
        raise HTTPException(status_code=400, detail="未知测试目标")

    # 显式传值优先,留空则用已保存配置。
    # 注意:embed/rerank 与 chat 使用两套独立的端点与密钥,
    # 回退时必须各回退各的,否则会拿对话 Key 去访问嵌入服务(401)。
    if req.target == "chat":
        base_url = (req.api_base_url or "").strip() or config.get("api_base_url")
        api_key = (req.api_key or "").strip() or config.get("api_key")
    else:
        base_url = (req.api_base_url or "").strip() \
            or config.get("embed_api_base_url")
        api_key = (req.api_key or "").strip() or config.get("embed_api_key")

    t0 = time.perf_counter()
    try:
        if req.target == "chat":
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url,
                            timeout=15, max_retries=0)
            models = client.models.list()
            ids = [m.id for m in models.data[:30]]
            ms = (time.perf_counter() - t0) * 1000
            return {
                "ok": True, "latency_ms": round(ms, 1),
                "detail": f"连接正常,可见 {len(ids)} 个模型",
                "models": ids,
            }

        if req.target == "embed":
            from openai import OpenAI
            model = req.model.strip() or config.get("embed_model")
            client = OpenAI(api_key=api_key, base_url=base_url,
                            timeout=20, max_retries=0)
            resp = client.embeddings.create(model=model, input=["连接测试"])
            dim = len(resp.data[0].embedding)
            ms = (time.perf_counter() - t0) * 1000
            return {
                "ok": True, "latency_ms": round(ms, 1),
                "detail": f"嵌入成功,向量维度 {dim}",
                "embedding_dim": dim,
            }

        # rerank:不是 OpenAI 标准接口,按服务商 /rerank 约定调用
        import requests
        model = req.model.strip() or config.get("rerank_model")
        url = base_url.rstrip("/") + "/rerank"
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": model, "query": "连接测试",
                  "documents": ["文档一", "文档二"]},
            timeout=15,
        )
        resp.raise_for_status()
        n = len(resp.json().get("results", []))
        ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": True, "latency_ms": round(ms, 1),
            "detail": f"重排成功,返回 {n} 条结果",
        }

    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        logger.warning(f"API 测试失败 [{req.target}]: {e}")
        return {
            "ok": False, "latency_ms": round(ms, 1),
            "detail": _classify_api_error(e),
        }


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
