"""不依赖外部 API 的单元测试：覆盖纯逻辑与边界条件"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []
def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        FAIL.append(name)

# ============ 1. _extract_json 容错 ============
from rag.query_planner import _extract_json, _as_str_list, QueryPlan

check("json/裸对象", _extract_json('{"a":1}') == {"a": 1})
check("json/代码块", _extract_json('```json\n{"a":1}\n```') == {"a": 1})
check("json/无语言标记代码块", _extract_json('```\n{"a":1}\n```') == {"a": 1})
check("json/前后有文字", _extract_json('分析：\n{"a":1}\n完毕') == {"a": 1})
check("json/损坏返回None", _extract_json("不是json") is None)
check("json/空返回None", _extract_json("") is None)
check("json/None输入", _extract_json(None) is None)
check("json/数组不接受", _extract_json('["a"]') is None)
check("json/截断不闭合", _extract_json('{"a":1,"b":"未闭合') is None)
check("json/嵌套对象", _extract_json('{"a":{"b":2}}') == {"a": {"b": 2}})

# ============ 2. _as_str_list 规整 ============
check("list/正常", _as_str_list(["a", "b"]) == ["a", "b"])
check("list/单字符串", _as_str_list("a") == ["a"])
check("list/去空白空值", _as_str_list(["a", "", "  ", None, "b"]) == ["a", "b"])
check("list/None", _as_str_list(None) == [])
check("list/数字混入", _as_str_list([1, "a"]) == ["1", "a"])
check("list/超限截断", len(_as_str_list([str(i) for i in range(20)], limit=5)) == 5)
check("list/dict输入不崩", _as_str_list({"a": 1}) == [])

# ============ 2b. 维度标签剥离 ============
from rag.query_planner import _strip_dimension_label as sdl

# 实测日志中出现过的真实形态
check("dim/维度A冒号", sdl("维度A：马克思关于生产力的论述") == "马克思关于生产力的论述")
check("dim/维度B全角冒号", sdl("维度B：历史对比分析") == "历史对比分析")
check("dim/带括号说明", sdl("维度A（经典原理映射）：某命题") == "某命题")
check("dim/括号包裹对应", sdl("（对应维度C）批判视角命题") == "批判视角命题")
check("dim/半角括号", sdl("(对应维度A)某命题") == "某命题")
check("dim/中文序号", sdl("维度一：某命题") == "某命题")
check("dim/无标签不动", sdl("马克思关于异化劳动的论述") == "马克思关于异化劳动的论述")
# 正文中间出现"维度"不应被误删
check("dim/中间的维度保留", sdl("从多个维度分析资本积累") == "从多个维度分析资本积累")
# 清洗后不能变空
check("dim/纯标签保留原文", sdl("维度A：") == "维度A：")
check("dim/空字符串", sdl("") == "")

# ============ 3. QueryPlan 降级与正常 ============
p0 = QueryPlan(question="测试问题")
check("plan/降级dense回退原问题", p0.dense_queries() == ["测试问题"])
check("plan/降级sparse回退原问题", p0.sparse_query() == "测试问题")
check("plan/降级block不含内部标签",
      "学科定位" not in p0.to_context_block() and "原始问题" in p0.to_context_block())

p1 = QueryPlan(question="Q", domain="政治经济学", level="本质层面", nature="批判分析",
               core_contradiction="矛盾X", propositions=["命题1", "命题2"],
               keywords=["范畴A", "范畴B"], missing_perspective="视角Z", analysis_ok=True)
check("plan/dense用命题", p1.dense_queries() == ["命题1", "命题2"])
check("plan/sparse空格拼接", p1.sparse_query() == "范畴A 范畴B")
blk = p1.to_context_block()
check("plan/block含各字段", all(x in blk for x in ["政治经济学", "矛盾X", "命题1", "范畴A", "视角Z"]))
check("plan/block标签避开'缺失'", "缺失" not in blk, blk)

# analysis_ok=True 但命题为空 -> dense 必须回退，不能返回空列表
p2 = QueryPlan(question="Q2", analysis_ok=True)
check("plan/ok但命题空时dense回退", p2.dense_queries() == ["Q2"])
check("plan/ok但关键词空时sparse回退", p2.sparse_query() == "Q2")

# ============ 4. RRF 融合 ============
from rag.retriever import HybridRetriever
from rag.config_store import get_config

rrf = HybridRetriever.rrf_combine


class _Stub:
    """只带 config 的桩对象

    rrf_combine / _deduplicate 只用到 self.config，用桩对象即可直接调用
    未绑定方法，无需构造真实检索器（那会加载十几万条索引）。
    """
    config = get_config()


_S = _Stub()

d = lambda i, t="txt": {"id": i, "text": t}
r = rrf(_S, [d(1), d(2)], [d(2), d(3)])
check("rrf/合并去重", [x["id"] for x in r] == [2, 1, 3], str([x["id"] for x in r]))
check("rrf/共识文档得分更高", r[0]["id"] == 2 and r[0]["rrf_score"] > r[1]["rrf_score"])

# 多通道（HyDE 场景）
r3 = rrf(_S, [d(1)], [d(1)], [d(1)], [d(9)])
check("rrf/三通道共识累加", r3[0]["id"] == 1 and abs(r3[0]["rrf_score"] - 3 * (1 / 61)) < 1e-9)

# 空正文的那份不应覆盖有正文的
r4 = rrf(_S, [{"id": 5, "text": ""}], [{"id": 5, "text": "有正文"}])
check("rrf/保留有正文的副本", r4[0]["text"] == "有正文")
r5 = rrf(_S, [{"id": 5, "text": "有正文"}], [{"id": 5, "text": ""}])
check("rrf/先有正文不被空覆盖", r5[0]["text"] == "有正文")

check("rrf/空输入", rrf(_S) == [])
check("rrf/全空列表", rrf(_S, [], []) == [])

# ============ 5. _deduplicate ============
dedup = HybridRetriever._deduplicate
res = dedup(_S, [d(1, "同样的文本"), d(2, "同样的文本"), d(3, "不同")])
check("dedup/按文本去重", [x["id"] for x in res] == [1, 3])

# ============ 6. _clean_text / _format_context ============
from rag.generator import RAGPipeline
ct = RAGPipeline._clean_text
check("clean/去blockquote", ct("> 引用行") == "引用行")
check("clean/去标题", ct("## 标题") == "标题")
check("clean/去分隔线", ct("a\n---\nb") == "a\n\nb", repr(ct("a\n---\nb")))
check("clean/空输入", ct("") == "" and ct(None) == "")

fc = RAGPipeline._format_context
# _format_context 是实例方法且内部调用 self._clean_text，
# 用一个只带该静态方法的桩对象充当 self，避免构造完整 RAGPipeline
stub = type("Stub", (), {"_clean_text": staticmethod(ct)})()
out = fc(stub, [{"text": "正文A", "metadata": {"chapter": "资本论"}}])
check("ctx/用书名标记", "《资本论》" in out and "[来源" not in out)
check("ctx/空文档列表", fc(stub, []) == "")
# metadata 缺失不应崩
check("ctx/无metadata不崩", isinstance(fc(stub, [{"text": "x"}]), str))

print("\n" + "=" * 50)
print(f"失败 {len(FAIL)} 项: {FAIL}" if FAIL else "全部通过")
sys.exit(1 if FAIL else 0)
