# -*- coding: utf-8 -*-
"""
请求级可观测性:关联 ID 与阶段计时

设计目标(问题 12 的全面优化):
  1. 每个 /api/chat 请求分配一个 request_id,贯穿 API 层、生成层、
     检索层、嵌入层的全部日志,一次请求的所有行可通过该 ID 串起来;
  2. StageTimer 对检索流水线的每个阶段(联网搜索、前置解构、检索、
     重排、首 token、生成、总耗时)做毫秒级计时,SSE done 事件带回
     前端展示,同时写入内存环形统计供设置页查看;
  3. 通过 contextvars 传递 request_id,线程内与协程内均可见,
     无需在各层函数间手工透传。

用法:
    from rag.telemetry import set_request_id, get_request_id, StageTimer

    set_request_id("abc123")            # 请求入口(线程/协程内)
    timer = StageTimer()                # 总计时自动开始
    timer.start("analyze")
    ...                                 # 一个阶段
    timer.end("analyze")
    timer.to_dict()                     # {"total_ms": 8.4, "analyze_ms": 3.2, ...}
"""

import contextvars
import logging
import time
import threading
from typing import Dict, Optional

# 环形性能统计的最大记录数
PERF_MAX_RECORDS = 200

_request_id_var: contextvars.ContextVar = contextvars.ContextVar(
    "request_id", default=None)


class RequestIdFilter(logging.Filter):
    """把当前上下文里的 request_id 注入每条日志记录

    挂到根 logger 的 handler 上后,日志格式里可用 %(request_id)s。
    CLI / 测试等没有请求上下文的场景输出 '-'。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        rid = _request_id_var.get()
        record.request_id = rid if rid else "-"
        return True


def set_request_id(request_id: str) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> Optional[str]:
    return _request_id_var.get()


class StageTimer:
    """一次请求的阶段计时器

    - 构造即开始总计时;
    - start(name) / end(name) 记录单个阶段的毫秒耗时;
    - to_dict() 输出 {"total_ms": ..., "<name>_ms": ...},键名
      已带 _ms 后缀,可直接序列化进 SSE / 统计接口。
    """

    def __init__(self):
        self._started = time.perf_counter()
        self._starts: Dict[str, float] = {}
        self._stages: Dict[str, float] = {}
        self._lock = threading.Lock()

    def start(self, name: str) -> None:
        with self._lock:
            self._starts[name] = time.perf_counter()

    def end(self, name: str) -> float:
        """结束某阶段,返回该阶段毫秒耗时"""
        now = time.perf_counter()
        with self._lock:
            begin = self._starts.pop(name, now)
            ms = (now - begin) * 1000.0
            self._stages[name] = ms
        return ms

    def mark(self, name: str, ms: float) -> None:
        """直接写入一个阶段的耗时(调用方自己计时时使用)"""
        with self._lock:
            self._stages[name] = ms

    def total_ms(self) -> float:
        return (time.perf_counter() - self._started) * 1000.0

    def stage_ms(self, name: str) -> float:
        return self._stages.get(name, 0.0)

    def to_dict(self) -> Dict[str, float]:
        out = {"total_ms": round(self.total_ms(), 1)}
        with self._lock:
            for name, ms in self._stages.items():
                out["%s_ms" % name] = round(ms, 1)
        return out

    def summarize(self) -> str:
        """一行人类可读的耗时摘要,直接用于日志"""
        parts = []
        for name, ms in self._stages.items():
            parts.append("%s=%.0fms" % (name, ms))
        return " ".join(parts) + " total=%.0fms" % self.total_ms()


# ── 请求性能环形统计(进程内,设置页展示) ─────────────────────────

class PerfRecorder:
    """记录最近 N 次请求的耗时快照,提供平均值聚合"""

    def __init__(self, max_records: int = PERF_MAX_RECORDS):
        self.max_records = max_records
        self._lock = threading.Lock()
        self._records: list = []

    def record(self, timings: Dict[str, float]) -> None:
        with self._lock:
            self._records.append(dict(timings))
            if len(self._records) > self.max_records:
                self._records = self._records[-self.max_records:]

    def summary(self) -> Dict:
        """最近请求的平均耗时(毫秒,保留 1 位小数)"""
        with self._lock:
            records = list(self._records)
        if not records:
            return {"requests": 0}

        keys = ["total_ms", "analyze_ms", "retrieve_ms",
                "first_token_ms", "generate_ms"]
        summary = {"requests": len(records)}
        for key in keys:
            values = [r[key] for r in records if key in r]
            if values:
                summary["avg_%s" % key] = round(
                    sum(values) / len(values), 1)
        return summary


# 进程级单例(设置页 stats 接口使用)
perf_recorder = PerfRecorder()
