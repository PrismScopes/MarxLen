# -*- coding: utf-8 -*-
"""
知识库版本热切换（在线端）

监听 data/releases.json 的原子替换事件：一旦发布指针指向新版本，
就在后台线程加载新索引（加载耗时约 2 秒），全部成功后才把在线
RAG 流水线的检索器做引用级原子替换。

设计保证：
  - 加载或校验失败 → 保持旧版本继续服务，只记日志；
  - 切换前在途请求持有的旧检索器对象不受影响（引用级 swap），
    旧对象延迟 60 秒后才释放连接与内存；
  - 版本指针写回旧版本（rollback）同样触发一次加载切换，
    所以"回滚"在线端也是同一套机制，无需重启。
"""

import logging
import os
import threading
import time
from typing import Optional

from .paths import RELEASES_PATH
from .release import resolve_index_dir

logger = logging.getLogger(__name__)

RETRY_DELAY = 5.0
DEBOUNCE_SECONDS = 5.0


class KBVersionWatcher:
    """发布指针监听器

    用法:
        watcher = KBVersionWatcher(pipeline)
        watcher.start()
        ...
        watcher.stop()
    """

    def __init__(self, pipeline, retry_delay: float = RETRY_DELAY):
        self.pipeline = pipeline
        self.retry_delay = retry_delay
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_change = 0.0
        self._reload_lock = threading.Lock()

    # ── 生命周期 ──────────────────────────────────────────────

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        logger.info("知识库热更新监听已启动（监听 %s）", RELEASES_PATH)
        self._thread = threading.Thread(
            target=self._run, name="kb-version-watcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        try:
            from watchfiles import watch
        except ImportError:
            logger.warning("watchfiles 不可用，知识库热更新关闭")
            return

        watch_dir = os.path.dirname(RELEASES_PATH) or "."
        watch_name = os.path.basename(RELEASES_PATH)
        try:
            for _changes in watch(
                    watch_dir,
                    watch_filter=lambda _c, p: os.path.basename(p) == watch_name,
                    stop_event=self._stop):
                if self._stop.is_set():
                    break
                self._on_pointer_changed()
        except Exception as e:
            logger.warning("知识库监听异常退出: %s", e)

    # ── 切换逻辑 ──────────────────────────────────────────────

    def _on_pointer_changed(self):
        """指针变化回调（防抖后进入重载）"""
        now = time.time()
        if now - self._last_change < DEBOUNCE_SECONDS:
            return
        self._last_change = now
        threading.Thread(target=self.reload_now, daemon=True).start()

    def reload_now(self) -> bool:
        """立即按当前指针重载知识库；返回是否完成切换

        同一时刻只允许一个重载流程（指针快速连续变化时，旧的
        重载直接放弃，由最后一次接管）。
        """
        if not self._reload_lock.acquire(blocking=False):
            return False
        try:
            return self._do_reload()
        finally:
            self._reload_lock.release()

    def _do_reload(self) -> bool:
        index_dir, build_id = resolve_index_dir()
        if index_dir is None or build_id is None:
            logger.warning("发布指针解析失败，保持当前知识库不变")
            return False

        current = getattr(self.pipeline, "kb_build_id", None)
        if build_id == current:
            logger.info("知识库版本未变化（%s），跳过", build_id)
            return True

        logger.info("检测到新知识库版本: %s，后台加载中...", build_id)
        try:
            from rag.retriever import HybridRetriever
            new_retriever = HybridRetriever(index_dir=index_dir)
        except Exception as e:
            logger.error("新版本加载失败，继续使用 %s: %s", current, e)
            return False

        logger.info("新版本加载完成，执行原子切换: %s -> %s", current, build_id)
        try:
            self.pipeline.swap_retriever(new_retriever, build_id)
        except Exception as e:
            logger.error("切换失败，保持旧版本: %s", e)
            try:
                new_retriever.close()
            except Exception:
                pass
            return False
        return True
