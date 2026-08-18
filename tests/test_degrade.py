"""定向验证：检索阶段抛异常时，用户是拿到回答还是拿到错误"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rag.generator import RAGPipeline

pipe = RAGPipeline()
pipe.answer_cache.get = lambda q, **kw: None

def run(label, patch_target, patch_fn):
    orig = getattr(*patch_target)
    setattr(patch_target[0], patch_target[1], patch_fn)
    try:
        evts, ans, srcs = [], "", 0
        try:
            for it in pipe.ask_stream("检索环节崩溃时系统是否还能回答用户的问题", top_k=3):
                evts.append(it["type"])
                if it["type"] == "token":
                    ans += it["content"]
                elif it["type"] == "done":
                    srcs = len(it.get("sources", []))
        except Exception as e:
            print(f"{label}: 异常穿透到调用方 -> {type(e).__name__}: {e}")
            print(f"  已产出事件={evts}  回答长度={len(ans)}")
            return "EXCEPTION"
        has_answer = len(ans) > 200 and "[错误]" not in ans
        print(f"{label}: 回答={len(ans)}字 来源={srcs}条 有效={has_answer}")
        return "OK" if has_answer else "NO_ANSWER"
    finally:
        setattr(patch_target[0], patch_target[1], orig)

print("=" * 60)
# 场景1：前置分析整体崩溃（模拟不可预期的内部错误）
r1 = run("[1] _build_plan 抛异常",
         (pipe, "_build_plan"),
         lambda q: (_ for _ in ()).throw(RuntimeError("planner 崩了")))

# 场景2：检索崩溃（模拟索引/DB 故障）
r2 = run("[2] retrieve_by_plan 抛异常",
         (pipe.retriever, "retrieve_by_plan"),
         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("索引崩了")))

# 场景3：嵌入 API 崩溃（真实场景：额度耗尽/网络断）
# 此时关键词通道仍可用，应仍能检索到资料
r3 = run("[3] dense_search 抛异常",
         (pipe.retriever, "dense_search"),
         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("嵌入API失败")))

# 场景4：BM25 崩溃，语义通道仍可用
r4 = run("[4] sparse_search 抛异常",
         (pipe.retriever, "sparse_search"),
         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("BM25失败")))

print("=" * 60)
results = {"1": r1, "2": r2, "3": r3, "4": r4}
print(f"结果: {results}")
bad = [k for k, v in results.items() if v != "OK"]
if bad:
    print(f"\n不合格场景: {bad}")
    print("局部故障导致整个请求失败；这些环节本可降级为无资料回答。")
    sys.exit(1)
print("\n全部场景均正确降级：局部故障不影响用户拿到回答。")
