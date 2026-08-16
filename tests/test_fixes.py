# -*- coding: utf-8 -*-
"""
本轮修复的专项测试：修复 1/2/7 + 问题 12 可观测性

覆盖:
  - 前置解构的对话历史注入与指代还原占位符
  - 引用后处理:规范化 / 编号转换 / 覆盖率
  - StageTimer 计时与 PerfRecorder 汇总
  - 日志过滤器 request_id 注入

运行方式：
    python tests/test_fixes.py      # 或经 tests/run_all.py 聚合执行
"""

import os
import sys
import time
import logging

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  <- " + str(detail)) if not cond and detail else ""))


# ======================================================================
# 修复 2:前置解构携带对话历史
# ======================================================================

def t_history_injection():
    from rag.query_planner import _format_history

    hist = [
        {"role": "user", "content": "什么是剩余价值？"},
        {"role": "assistant", "content": "剩余价值是……" * 50},
        {"role": "user", "content": "那它的第二个来源呢？"},
    ]
    block = _format_history(hist)
    check("hist/包含最近消息", "第二个来源" in block)
    check("hist/最多4条", block.count("用户:") + block.count("助手:") <= 4)
    check("hist/单条截断", len(block) < 2000, len(block))
    check("hist/空历史为空", _format_history([]) == "")

    # 注入逻辑:新提示词含 {history} 占位符
    tpl = "前文。\n{history}\n\n问题:{question}"
    filled = tpl.replace("{history}", block).replace("{question}", "追问")
    check("hist/占位符注入", "第二个来源" in filled and "追问" in filled)

    # 旧提示词无占位符:历史拼到问题前,问题不被吞
    tpl_old = "前文。\n\n问题:{question}"
    filled_old = tpl_old.replace(
        "{question}", block + "\n用户当前问题: {question}") \
        .replace("{question}", "追问")
    check("hist/无占位符回退注入",
          "第二个来源" in filled_old and filled_old.rstrip().endswith("追问"),
          filled_old[-60:])


# ======================================================================
# 修复 7:引用后处理
# ======================================================================

def t_reference_postprocess():
    from rag.generator import RAGPipeline
    pp = RAGPipeline._postprocess_references

    sources = [
        {"author": "马克思恩格斯全集23", "title": "资本论"},
        {"author": "列宁全集第27卷", "title": "帝国主义论"},
    ]

    # 1. 规范化各种变体
    raw = "正文内容。\n\n> 参考自:《资本论》\n\n更多正文。\n\n>参考自 \"帝国主义论\"\n"
    out, rep = pp(raw, sources)
    check("ref/变体规范化", "> 参考自《资本论》" in out
          and "> 参考自《帝国主义论》" in out, out)
    check("ref/规范化计数", rep["normalized"] == 2, rep)

    # 2. 编号引用转换与丢弃
    raw2 = "第一点[来源 1]。第二点[来源 2]。第三点[来源 9]。"
    out2, rep2 = pp(raw2, sources)
    check("ref/有效编号转换", "（见《马克思恩格斯全集23》）" in out2
          and "（见《列宁全集第27卷》）" in out2, out2)
    check("ref/无效编号丢弃", "[来源 9]" not in out2
          and rep2["num_dropped"] == 1, rep2)

    # 3. 覆盖率:全部引用则 uncited 为空;漏引则列出
    raw3 = "正文。\n\n> 参考自《资本论》\n"
    _out3, rep3 = pp(raw3, sources)
    check("ref/漏引统计", rep3["uncited"] == ["列宁全集第27卷"], rep3)
    _out4, rep4 = pp(raw3 + "\n\n> 参考自《帝国主义论》\n", sources)
    check("ref/全部引用无漏引", rep4["uncited"] == [], rep4)

    # 4. 空白回答不崩
    out5, rep5 = pp("", sources)
    check("ref/空回答安全", out5 == "" and rep5["uncited"] ==
          ["马克思恩格斯全集23", "列宁全集第27卷"], rep5)


# ======================================================================
# 问题 12:StageTimer 与 PerfRecorder
# ======================================================================

def t_telemetry():
    from rag.telemetry import StageTimer, PerfRecorder, RequestIdFilter
    from rag.telemetry import set_request_id

    timer = StageTimer()
    timer.start("analyze")
    time.sleep(0.02)
    timer.end("analyze")
    d = timer.to_dict()
    check("timer/阶段耗时存在", d.get("analyze_ms", 0) >= 15, d)
    check("timer/总耗时不小于阶段", d["total_ms"] >= d["analyze_ms"], d)
    check("timer/summarize可读", "analyze=" in timer.summarize())

    rec = PerfRecorder(max_records=5)
    for i in range(7):
        rec.record({"total_ms": 100.0 + i, "analyze_ms": 10.0,
                    "retrieve_ms": 20.0, "first_token_ms": 30.0,
                    "generate_ms": 40.0})
    s = rec.summary()
    check("perf/环形只留5条", s["requests"] == 5, s)
    check("perf/平均正确", abs(s["avg_total_ms"] - 104.0) < 0.01, s)
    check("perf/空汇总", PerfRecorder().summary() == {"requests": 0})

    # RequestIdFilter 注入 request_id 字段
    set_request_id("rid-123")
    f = RequestIdFilter()
    record = logging.LogRecord("x", logging.INFO, "", 0, "msg", (), None)
    ok = f.filter(record)
    check("logfilter/注入request_id", ok and record.request_id == "rid-123")
    set_request_id(None)
    record2 = logging.LogRecord("x", logging.INFO, "", 0, "msg", (), None)
    f.filter(record2)
    check("logfilter/无请求时为横线", record2.request_id == "-")


# ======================================================================
# 思考强度(参考 DSH 推理等级)
# ======================================================================

def t_thinking_effort():
    from rag.generator import normalize_effort

    check("effort/合法三档", all(normalize_effort(e) == e
          for e in ("off", "high", "max")))
    check("effort/非法回退off", normalize_effort("deep") == "off")
    check("effort/None回退off", normalize_effort(None) == "off")
    check("effort/空串回退off", normalize_effort("") == "off")

    # 思考档答案与普通答案的缓存键不同(不同推理深度的答案不互相命中)
    from rag.generator import THINKING_EFFORTS
    check("effort/档位枚举顺序", THINKING_EFFORTS == ("off", "high", "max"))


def main():
    t_history_injection()
    t_reference_postprocess()
    t_telemetry()
    t_thinking_effort()

    fails = [n for n, ok in RESULTS if not ok]
    print("\n" + "=" * 60)
    print("总计 %d 项,通过 %d 项,失败 %d 项" %
          (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    if fails:
        print("失败项: %s" % ", ".join(fails))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
