"""集成测试：真实索引 + 真实 API，检查端到端链路与输出合规性"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []
def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        FAIL.append(name)

from rag.generator import RAGPipeline

print("初始化 pipeline...")
t0 = time.time()
pipe = RAGPipeline()
print(f"初始化耗时 {time.time() - t0:.1f}s\n")

# ============ 1. 提示词加载 ============
check("prompt/main非空", len(pipe.main_prompt) > 500, f"{len(pipe.main_prompt)}字")
check("prompt/rag非空", len(pipe.rag_prompt) > 500, f"{len(pipe.rag_prompt)}字")
check("prompt/main含输入契约", "输入数据契约" in pipe.main_prompt)
check("prompt/main含引用格式", "参考自" in pipe.main_prompt)
check("prompt/rag含JSON契约", "core_contradiction" in pipe.rag_prompt)
check("prompt/rag含question占位", "{question}" in pipe.rag_prompt)
# 主提示词进 system 位后，模板里不应还残留旧的硬编码角色设定
check("prompt/无重复角色设定", pipe.main_prompt.count("## 你的角色") == 1)

# ============ 2. 索引一致性 ============
r = pipe.retriever
vec_n = r.store.count()
sql_n = r.store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
bm25_n = len(r.docs)
print(f"\n向量={vec_n} SQLite={sql_n} BM25={bm25_n}")
check("index/BM25与SQLite一致", bm25_n == sql_n, f"{bm25_n} vs {sql_n}")
check("index/向量与SQLite一致", vec_n == sql_n, f"差 {abs(vec_n - sql_n)} 条")

# BM25 的 docs[i]['id'] 必须能在 SQLite 命中
ids = [r.docs[i]["id"] for i in (0, bm25_n // 2, bm25_n - 1)]
found = r.store.conn.execute(
    f"SELECT COUNT(*) FROM documents WHERE id IN ({','.join('?' * len(ids))})", ids
).fetchone()[0]
check("index/BM25映射有效", found == len(ids), f"{found}/{len(ids)}")

# ============ 3. 前置分析（真实 API）============
q = "为什么现在的年轻人普遍感到工作压力大却难以积累财富"
print(f"\n前置分析: {q}")
t0 = time.time()
plan = pipe._build_plan(q)
print(f"耗时 {time.time() - t0:.1f}s")
print(f"  ok={plan.analysis_ok} 域={plan.domain} 层={plan.level} 性质={plan.nature}")
print(f"  矛盾={plan.core_contradiction[:60]}")
print(f"  命题={len(plan.propositions)} 关键词={len(plan.keywords)}")

check("plan/分析成功", plan.analysis_ok)
check("plan/有核心矛盾", len(plan.core_contradiction) > 5)
check("plan/有命题", len(plan.propositions) >= 2, str(len(plan.propositions)))
check("plan/有关键词", len(plan.keywords) >= 2, str(len(plan.keywords)))
check("plan/命题是句子非关键词", all(len(p) > 8 for p in plan.propositions))

# ============ 4. 多通道检索 ============
print("\n多通道检索...")
t0 = time.time()
docs = r.retrieve_by_plan(plan, top_k=5, fetch_k=30)
print(f"耗时 {time.time() - t0:.1f}s，返回 {len(docs)} 条")
check("retr/有结果", len(docs) > 0)
check("retr/均有正文", all(d.get("text") for d in docs))
check("retr/有rerank分", all("rerank_score" in d for d in docs))
if docs:
    top = docs[0].get("rerank_score", 0)
    print(f"  最高 rerank={top:.4f}  来源={docs[0]['metadata'].get('source', '')}")
    check("retr/相关度合理", top > 0.3, f"{top:.4f}")
    check("retr/有来源元数据",
          any(d["metadata"].get("source") or d["metadata"].get("chapter") for d in docs))

# 降级路径：analysis_ok=False 必须能正常检索
from rag.query_planner import QueryPlan
d2 = r.retrieve_by_plan(QueryPlan(question="剩余价值"), top_k=3, fetch_k=20)
check("retr/降级路径可用", len(d2) > 0)

# ============ 5. 端到端流式输出 ============
print("\n端到端（普通模式）...")
pipe.answer_cache.get = lambda q: None  # 绕过缓存，确保真实生成

evts, answer = [], ""
t0 = time.time()
for item in pipe.ask_stream(q, top_k=5, fetch_k=30):
    evts.append(item["type"])
    if item["type"] == "token":
        answer += item["content"]
print(f"耗时 {time.time() - t0:.1f}s，{len(answer)} 字")

check("e2e/有token", "token" in evts)
check("e2e/有done", evts[-1] == "done", str(evts[-3:]))
check("e2e/done仅一次", evts.count("done") == 1)
check("e2e/回答够长", len(answer) > 300, f"{len(answer)}字")
check("e2e/无API错误", "[错误]" not in answer)

# ============ 6. 输出合规性（回归上次修的两个 bug）============
leaks = ["原始问题", "学科定位", "范畴层级", "问题性质", "经典命题映射",
         "缺失视角", "需补充展开的视角", "参考文档】", "认知锚点",
         "第一步", "第二步", "第七步", "内部认知流程"]
hit = [w for w in leaks if w in answer]
check("out/无字段名泄露", not hit, str(hit))

check("out/无编号引用", not re.search(r"\[来源\s*\d+\]", answer))

quote_lines = [l.strip() for l in answer.split("\n") if l.strip().startswith(">")]
bad = [l for l in quote_lines if not re.match(r"^>\s*参考自《.+?》\s*$", l)]
print(f"  引用行 {len(quote_lines)} 条，违规 {len(bad)} 条")
if bad:
    for b in bad[:3]:
        print(f"    违规: {b[:70]}")
check("out/引用行格式合规", not bad, f"{len(bad)}条违规")
check("out/有引用来源", len(quote_lines) > 0, "未标注任何来源")

# blockquote 前必须空行，否则 markdown 渲染失败
lines = answer.split("\n")
no_blank = [i for i, l in enumerate(lines)
            if l.strip().startswith(">") and i > 0
            and lines[i - 1].strip() and not lines[i - 1].strip().startswith(">")]
check("out/引用前有空行", not no_blank, f"{len(no_blank)}处缺空行")

print("\n" + "=" * 50)
print(f"失败 {len(FAIL)} 项: {FAIL}" if FAIL else "全部通过")
sys.exit(1 if FAIL else 0)
