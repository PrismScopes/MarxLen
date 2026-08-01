"""API 层测试：SSE 事件契约、对话管理、设置接口、并发与边界"""
import os
import sys
import json
import collections
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []
def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        FAIL.append(name)

from fastapi.testclient import TestClient
import api.routes as routes
from api.main import app


def stream_chat(c, payload):
    """收集一次 SSE 会话的事件与数据"""
    ev = collections.Counter()
    order, toks, done = [], [], {}
    refs = []
    with c.stream("POST", "/api/chat", json=payload) as r:
        status = r.status_code
        last = ""
        for line in r.iter_lines():
            if line.startswith("event: "):
                last = line[7:].strip()
                ev[last] += 1
                if last not in ("token", "thinking"):
                    order.append(last)
            elif line.startswith("data: "):
                raw = line[6:].strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    check(f"sse/data必须是合法JSON({last})", False, raw[:60])
                    continue
                if last == "token":
                    toks.append(d.get("content", ""))
                elif last == "done":
                    done = d
                elif last == "search_result":
                    refs = d.get("references", [])
    return status, ev, order, "".join(toks), done, refs


with TestClient(app) as c:
    # ============ 1. 基础接口 ============
    h = c.get("/api/health").json()
    check("api/health", h.get("status") == "ok" and h.get("rag_initialized") is True, str(h))

    # GET /api/settings 返回 schema 驱动的完整描述，供前端自动渲染
    sc = c.get("/api/settings").json()
    items = sc["items"]
    keys = [i["key"] for i in items]
    check("api/settings含分类", len(sc.get("categories", [])) > 0)
    check("api/settings含项目", len(keys) > 20, str(len(keys)))
    check("api/settings含核心项", {"model", "top_k", "temperature"} <= set(keys))
    check("api/item字段完整",
          all({"key", "label", "type", "value", "default", "category"} <= set(i) for i in items))
    # 敏感项不能把真实 key 明文下发给前端
    secrets = [i for i in items if i.get("secret")]
    check("api/敏感项已脱敏",
          bool(secrets) and all("sk-" not in str(i["value"]) or "*" in str(i["value"])
                                for i in secrets))

    # PUT 更新 + reset 回默认
    r = c.put("/api/settings", json={"updates": {"top_k": 7}})
    check("api/put成功", r.status_code == 200 and r.json()["settings"]["top_k"] == 7, r.text[:120])
    r = c.put("/api/settings", json={"updates": {"top_k": 99999, "不存在的键": 1}})
    check("api/越界被钳制", r.json()["settings"]["top_k"] <= 50, r.text[:120])
    check("api/未知键被忽略", "不存在的键" not in r.json()["settings"])
    r = c.post("/api/settings/reset", json={"key": "top_k"})
    check("api/reset单项", r.status_code == 200 and r.json()["settings"]["top_k"] == 8, r.text[:120])

    stats = c.get("/api/settings/stats").json()
    check("api/stats有文档数", stats.get("document_count", 0) > 0, str(stats))

    check("api/models", c.get("/api/models").status_code == 200)

    # ============ 2. 普通问答 SSE ============
    print("\n[普通模式]")
    status, ev, order, ans, done, _ = stream_chat(c, {"question": "什么是商品拜物教"})
    print(f"  events={dict(ev)} order={order}")
    check("chat/200", status == 200)
    check("chat/有token", ev["token"] > 0)
    check("chat/done结尾", order and order[-1] == "done")
    check("chat/done含conversation_id", bool(done.get("conversation_id")))
    check("chat/done含sources", isinstance(done.get("sources"), list))
    check("chat/done含references字段", "references" in done)
    check("chat/无error事件", ev["error"] == 0)
    conv_id = done.get("conversation_id")

    # ============ 3. 多轮对话上下文 ============
    print("\n[多轮对话]")
    s2, ev2, o2, ans2, done2, _ = stream_chat(
        c, {"question": "它和异化有什么关系", "conversation_id": conv_id})
    check("chat/复用conversation_id", done2.get("conversation_id") == conv_id)
    detail = c.get(f"/api/conversations/{conv_id}").json()
    check("conv/消息累计4条", len(detail["messages"]) == 4, str(len(detail["messages"])))
    roles = [m["role"] for m in detail["messages"]]
    check("conv/角色交替", roles == ["user", "assistant", "user", "assistant"], str(roles))
    check("conv/assistant有sources", any(m.get("sources") for m in detail["messages"]))

    # ============ 4. 对话列表与删除 ============
    lst = c.get("/api/conversations?limit=50").json()
    check("conv/列表含新对话", any(x["id"] == conv_id for x in lst))
    check("conv/删除成功", c.delete(f"/api/conversations/{conv_id}").status_code == 200)
    check("conv/删除后404", c.get(f"/api/conversations/{conv_id}").status_code == 404)
    check("conv/删除不存在404", c.delete("/api/conversations/nonexistent-id").status_code == 404)

    # ============ 5. 边界输入 ============
    print("\n[边界输入]")
    s, ev, o, a, d, _ = stream_chat(c, {"question": "马克思"})   # 短问题 -> 跳过前置分析
    check("edge/短问题可用", ev["token"] > 0 and o[-1] == "done")

    r = c.post("/api/chat", json={"question": "x", "conversation_id": "不存在的ID"})
    check("edge/无效会话404", r.status_code == 404, str(r.status_code))

    r = c.post("/api/chat", json={})
    check("edge/缺question返回422", r.status_code == 422, str(r.status_code))

    # ============ 6. 联网搜索事件契约（打桩，不依赖外网）============
    print("\n[联网搜索]")
    import rag.generator as g
    _orig_ws, _orig_fmt = g.web_search, g.format_search_results
    g.web_search = lambda q, **k: [
        {"title": "T1", "body": "B1", "href": "http://a"},
        {"title": "T2", "body": "B2", "href": "http://b"}]
    g.format_search_results = lambda r: "【联网搜索结果】：T1 T2"
    try:
        s, ev, o, a, d, refs = stream_chat(
            c, {"question": "如何看待当代平台经济中的劳动关系", "search_mode": True})
        print(f"  events={dict(ev)} order={o}")
        check("web/发出search_result", ev["search_result"] == 1, str(dict(ev)))
        check("web/search_result先于done", o.index("search_result") < o.index("done"))
        check("web/refs含url字段", refs and all("url" in x for x in refs), str(refs[:1]))
        check("web/done含references", len(d.get("references", [])) == 2, str(d.get("references")))
    finally:
        g.web_search, g.format_search_results = _orig_ws, _orig_fmt

    # ============ 7. 缓存路径 ============
    print("\n[缓存命中]")
    qc = "缓存路径专用测试问题：什么是生产力"
    stream_chat(c, {"question": qc})              # 首次，写入缓存
    s, ev, o, a2, d2, _ = stream_chat(c, {"question": qc})  # 第二次应命中
    check("cache/命中仍有done", o[-1] == "done")
    check("cache/命中有内容", len(a2) > 50)
    check("cache/命中不报错", ev["error"] == 0)

    # ============ 8. 前置分析失败时的降级 ============
    print("\n[降级路径]")
    pipe = routes.rag_pipeline
    _orig_plan = pipe._build_plan
    pipe._build_plan = lambda q: (_ for _ in ()).throw(RuntimeError("模拟分析崩溃"))
    try:
        s, ev, o, a, d, _ = stream_chat(c, {"question": "分析失败时能否正常回答用户的问题"})
        check("degrade/分析崩溃仍有响应", ev["token"] > 0 or ev["error"] > 0, str(dict(ev)))
        check("degrade/流正常结束", o and o[-1] in ("done", "error"), str(o))
    finally:
        pipe._build_plan = _orig_plan

    # ============ 9. 并发请求 ============
    print("\n[并发]")
    results = {}
    def worker(i):
        try:
            s, ev, o, a, d, _ = stream_chat(c, {"question": f"并发测试问题{i}：什么是阶级"})
            results[i] = (ev["token"] > 0, o[-1] if o else None)
        except Exception as e:
            results[i] = ("EXC", str(e)[:80])
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    [t.start() for t in ts]
    [t.join(timeout=180) for t in ts]
    print(f"  {results}")
    check("concur/全部完成", len(results) == 3, str(len(results)))
    check("concur/均成功", all(v[0] is True and v[1] == "done" for v in results.values()), str(results))

print("\n" + "=" * 50)
print(f"失败 {len(FAIL)} 项: {FAIL}" if FAIL else "全部通过")
sys.exit(1 if FAIL else 0)
