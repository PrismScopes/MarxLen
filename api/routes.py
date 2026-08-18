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
    ChatRequest,
    Conversation, ConversationDetail, ConversationRenameRequest,
    MessageVariants, SwitchVariantRequest,
    ModelOption, ModelUpdateRequest, ModelAddRequest,
    SettingsUpdateRequest, SettingsResetRequest, CacheClearRequest,
    ApiTestRequest, StatsResponse,
    parse_model_list, serialize_model_list,
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
        """SSE 事件生成器

        数据累积与落库全部在生成线程(run_generator)内完成;
        本协程只做转发,客户端刷新断开(CancelledError)不影响落库。
        """
        try:
            from rag.telemetry import set_request_id, perf_recorder
            set_request_id(request_id)   # 协程侧日志带 request_id
            # 以下变量仅用于 done 事件回显,内容由生成线程经队列带过来
            sources_data = []
            search_refs = []
            done_timings = None
            done_ref_report = None

            # 首帧即带回 request_id:前端可据此把日志与本次会话对应起来
            yield {
                "event": "meta",
                "data": json.dumps({"request_id": request_id}, ensure_ascii=False),
            }

            # ── 流开始前先插入占位助手消息 ──────────────────────
            # 生成过程可能持续几十秒,期间用户刷新页面时后端必须已有一条
            # 助手消息存在,否则"只有用户消息、没有回答"的会话看起来像数据
            # 丢失。这里先插入空消息拿到 message_id,生成线程在跑的同时
            # 用节流快照不断更新它的内容,结束时做最终落库。
            placeholder = conv_store.add_message(
                conv_id, "assistant", "", sources=[],
                parent_id=user_msg_id, branch=branch,
            )
            assistant_msg_id = placeholder.id
            assistant_variant_count = placeholder.variant_count
            assistant_variant_index = placeholder.variant_index

            fetch_k = int(config.get("fetch_k"))
            top_k = int(config.get("top_k"))

            loop = asyncio.get_running_loop()
            queue = asyncio.Queue(maxsize=int(config.get("stream_queue_size")))

            # ── 生成线程:累积 + 节流快照 + 最终落库 ─────────────
            # 关键设计:数据累积与落库全部发生在这个线程里,而不是 SSE
            # 协程里。客户端刷新断开连接只会取消协程(CancelledError),
            # 线程照常跑到生成结束并完成落库——完整答案、来源、思考过程
            # 最终一定写入数据库,不会因为刷新而丢失。
            def run_generator():
                from rag.telemetry import set_request_id as _set_rid
                _set_rid(request_id)     # 生成线程侧日志带 request_id
                full_answer = ""
                thinking_content = ""
                sources_data = []
                stage_detail = None
                # 快照节流:思考/正文累积期间周期性落库。
                # 频率取"时间间隔"与"字数增量"两者先到者。
                _snapshot_last = time.monotonic()
                _snapshot_len = 0
                _INTERVAL = 2.0     # 秒
                _CHARS = 600        # 思考或正文累积字数

                def _snapshot(force=False):
                    nonlocal _snapshot_last, _snapshot_len
                    grown = len(full_answer) + len(thinking_content) - _snapshot_len
                    if not force and \
                            time.monotonic() - _snapshot_last < _INTERVAL \
                            and grown < _CHARS:
                        return
                    _snapshot_last = time.monotonic()
                    _snapshot_len = len(full_answer) + len(thinking_content)
                    try:
                        conv_store.update_message(
                            conv_id, assistant_msg_id, full_answer,
                            sources=[], thinking_content=thinking_content,
                            stage_detail=stage_detail,
                        )
                    except Exception as e:
                        logger.warning("生成中快照保存失败: %s", e)

                def _finalize():
                    """生成结束(无论正常/异常)时的最终落库,带完整来源"""
                    try:
                        conv_store.update_message(
                            conv_id, assistant_msg_id, full_answer,
                            sources=sources_data, thinking_content=thinking_content,
                            stage_detail=stage_detail,
                        )
                    except Exception as e:
                        logger.warning("最终落库失败: %s", e)

                def _put(item):
                    fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                    try:
                        fut.result(timeout=5)  # 5秒超时防止死锁
                    except Exception:
                        return False  # 消费端(SSE 协程)已断开
                    return True

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
                        t = item.get("type")
                        if t == "stage":
                            if item.get("detail"):
                                # 解构卡片数据只出现一次（analyze 完成时）
                                stage_detail = item.get("detail")
                        elif t == "thinking":
                            if not thinking_content:
                                # 首条思考内容立即落库,消除节流空窗
                                _snapshot(force=True)
                            thinking_content += item["content"]
                            _snapshot()
                        elif t == "token":
                            if not full_answer and not thinking_content:
                                # 首 token(含缓存命中直接返回全文)立即落库
                                _snapshot(force=True)
                            full_answer += item["content"]
                            _snapshot()
                        elif t == "done":
                            sources_data = item.get("sources", [])
                            # 附带给协程回显用:思考内容与字数
                            # （协程已不再累积,值从线程带过去）
                            item["thinking_content"] = thinking_content
                            item["answer_len"] = len(full_answer)
                        if not _put(item):
                            break  # SSE 断开:停止转发,但最终落库照常执行
                    _finalize()
                except Exception as e:
                    _finalize()  # 生成异常也落库已生成的部分
                    _put({"type": "error", "detail": str(e)})

            thread = threading.Thread(target=run_generator, daemon=True)
            thread.start()

            # 异步从队列读取并转发给前端。
            # 数据累积与落库都在生成线程里,协程只做转发:
            # 客户端刷新断开(CancelledError)时协程退出,线程照常落库,
            # 完整回答不会丢。
            had_thinking = False
            thinking_done_sent = False
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=float(config.get("stream_timeout"))
                        )
                    except asyncio.TimeoutError:
                        # 持续无输出超过 stream_timeout:明确告知用户,
                        # 而不是让前端永远停在"处理中"或静默结束。
                        # 落库由生成线程负责,这里只通知前端。
                        yield {
                            "event": "error",
                            "data": json.dumps({
                                "detail": "生成超时,请重试(可尝试降低思考强度)"
                            }, ensure_ascii=False),
                        }
                        return
                    if item["type"] == "done":
                        sources_data = item.get("sources", [])
                        search_refs = item.get("search_refs", [])
                        done_timings = item.get("timings") or {}
                        done_ref_report = item.get("ref_report") or {}
                        done_thinking = item.get("thinking_content") or ""
                        done_answer_len = item.get("answer_len") or 0
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
                        had_thinking = True
                        yield {
                            "event": "thinking",
                            "data": json.dumps({"content": item["content"]}, ensure_ascii=False),
                        }
                    elif item["type"] == "token":
                        # 首个正文 token 意味着思考阶段结束，通知前端收起思考面板
                        if had_thinking and not thinking_done_sent:
                            thinking_done_sent = True
                            yield {"event": "thinking_done", "data": "{}"}
                        yield {
                            "event": "token",
                            "data": json.dumps({"content": item["content"]}, ensure_ascii=False),
                        }
            except asyncio.CancelledError:
                # 客户端断开:停止转发即可。生成线程会继续跑到结束并落库,
                # 完整回答与来源不会因为刷新而丢失。
                return

            # 发送完成事件（含来源、联网搜索结果、conversation_id）
            # message_id / variant_* 让前端立刻能渲染版本切换器，
            # 不必再多请求一次对话详情。
            # timings 为本次请求各阶段耗时（总耗时/解构/检索/首 token/生成），
            # ref_report 为引用后处理的覆盖统计。
            # 注意:最终落库已在生成线程的 _finalize() 完成,
            # 这里不再写数据库。
            try:
                from rag.telemetry import perf_recorder
                if done_timings:
                    perf_recorder.record(done_timings)
            except Exception:
                pass
            # done_timings 在流超时/客户端断开时可能为 None,
            # 统一用空字典兜底,避免完成日志本身崩溃
            _timings = done_timings or {}
            logger.info("请求完成 [total=%.0fms analyze=%.0fms retrieve=%.0fms "
                        "first_token=%.0fms generate=%.0fms] [字数=%d]",
                        _timings.get("total_ms", 0),
                        _timings.get("analyze_ms", 0),
                        _timings.get("retrieve_ms", 0),
                        _timings.get("first_token_ms", 0),
                        _timings.get("generate_ms", 0),
                        done_answer_len)

            yield {
                "event": "done",
                "data": json.dumps({
                    "conversation_id": conv_id,
                    "sources": sources_data,
                    "references": search_refs,
                    "thinking_content": done_thinking if effort != "off" else "",
                    "message_id": assistant_msg_id,
                    "user_message_id": user_msg_id,
                    "variant_count": assistant_variant_count,
                    "variant_index": assistant_variant_index,
                    "request_id": request_id,
                    "timings": done_timings,
                    "ref_report": done_ref_report or {},
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

@router.get("/models")
async def list_models():
    """读取可用模型列表与当前生效模型

    返回 {current, models}:
      current - 当前正在使用的模型 ID（设置页切换后写入 OPENAI_MODEL，
                刷新页面后前端据此恢复选择,而不是默认选列表第一个）
      models  - 候选模型列表。来自设置项 model_list（落库为 .env 的
                OPENAI_MODEL_LIST），格式：id1:显示名1,id2:显示名2。
                留空时只返回当前模型这一项。
    """
    models = [
        ModelOption(id=it["id"], name=it["name"], provider="DeepSeek")
        for it in parse_model_list(config.get("model_list") or "")
    ]
    if not models:
        # 没有配置候选列表，只显示当前模型
        configured_model = config.get("model")
        models = [ModelOption(id=configured_model, name=configured_model,
                              provider="DeepSeek")]
    return {
        "current": config.get("model"),
        "models": [m.model_dump() for m in models],
    }


# ================================================================
# 5b. POST /api/models - 添加模型(GUI 直接管理)
# ================================================================

@router.post("/models")
async def add_model(req: ModelAddRequest):
    """把模型追加到 OPENAI_MODEL_LIST(重复 id 则更新显示名)

    添加后立即可在输入框的模型选择器中切换,无需重启。
    """
    model_id = (req.id or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="模型 ID 不能为空")
    name = (req.name or "").strip() or model_id

    items = parse_model_list(config.get("model_list") or "")
    existed = any(it["id"] == model_id for it in items)
    items = [it for it in items if it["id"] != model_id]
    items.append({"id": model_id, "name": name})

    config.update({"model_list": serialize_model_list(items)})
    logger.info("模型列表: %s %s", "更新" if existed else "添加", model_id)
    return {"ok": True, "added": not existed,
            "models": parse_model_list(config.get("model_list") or "")}


# ================================================================
# 5c. DELETE /api/models/{model_id} - 移除模型
# ================================================================

@router.delete("/models/{model_id}")
async def remove_model(model_id: str):
    """从 OPENAI_MODEL_LIST 移除指定模型;当前正在使用的模型禁止移除"""
    items = parse_model_list(config.get("model_list") or "")
    kept = [it for it in items if it["id"] != model_id]
    if len(kept) == len(items):
        return {"ok": True, "removed": False,
                "models": parse_model_list(config.get("model_list") or "")}
    if model_id == config.get("model"):
        raise HTTPException(status_code=400,
                            detail="不能移除当前正在使用的模型")

    config.update({"model_list": serialize_model_list(kept)})
    logger.info("模型列表: 移除 %s", model_id)
    return {"ok": True, "removed": True,
            "models": parse_model_list(config.get("model_list") or "")}


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


# ================================================================
# 12. POST /api/settings/backup - 备份用户数据
# ================================================================

@router.post("/settings/backup")
async def create_backup():
    """把用户数据(对话/配置/密钥/缓存)打包为带时间戳的 zip

    返回 {ok, path, files, size}。备份文件落在项目根目录
    backup_<时间戳>.zip,该目录已被 .gitignore 排除。
    注意:备份包含 API 密钥,请妥善保管,勿外传。
    """
    try:
        result = settings_store.backup(rag_pipeline=rag_pipeline)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("error", "备份失败"))
        return result
    except Exception as e:
        logger.exception("备份失败")
        raise HTTPException(status_code=500, detail=f"备份失败: {e}")
