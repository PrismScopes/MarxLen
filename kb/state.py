# -*- coding: utf-8 -*-
"""
管线状态库（data/kb_state.db）

两张表：
  1. pipeline_checkpoint —— 构建过程的断点记录。构建幂等且按
     批次推进，崩溃后重跑时已完成的批次被跳过（或直接从嵌入
     缓存命中），不会重复烧钱。
  2. embed_cache —— 文本级嵌入缓存。键为文本哈希，与 chunk 的
     CAS 身份同源：相同文本跨文件、跨 build 只嵌入一次。模型或
     维度变化时整表作废（缓存必须与服务端向量空间严格一致）。

注意：这是数据工程的内部状态，与在线端的 cache_embeddings.db
（查询向量缓存）是两个东西，互不干扰。
"""

import json
import logging
import os
import sqlite3
import struct
import threading
import time
from typing import Dict, List, Optional

from .paths import KB_STATE_DB

logger = logging.getLogger(__name__)


class PipelineState:
    """构建 checkpoint：记录每个 build 各阶段的完成状态与元数据"""

    def __init__(self, db_path: str = KB_STATE_DB):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_checkpoint (
                build_id   TEXT PRIMARY KEY,
                step       TEXT NOT NULL,
                payload    TEXT DEFAULT '{}',
                updated_at REAL NOT NULL
            )
        """)
        self._conn.commit()

    def set_step(self, build_id: str, step: str, payload: Optional[Dict] = None):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pipeline_checkpoint "
                "(build_id, step, payload, updated_at) VALUES (?, ?, ?, ?)",
                (build_id, step, json.dumps(payload or {}, ensure_ascii=False),
                 time.time()),
            )
            self._conn.commit()

    def get_step(self, build_id: str) -> Optional[Dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT step, payload FROM pipeline_checkpoint WHERE build_id = ?",
                (build_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return {"step": row[0], "payload": json.loads(row[1] or "{}")}
        except json.JSONDecodeError:
            return {"step": row[0], "payload": {}}

    def drop(self, build_id: str):
        with self._lock:
            self._conn.execute(
                "DELETE FROM pipeline_checkpoint WHERE build_id = ?", (build_id,))
            self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


class TextEmbeddingCache:
    """文本级嵌入缓存：text_sha -> (model, dim, vector)

    键用文本哈希而非文本本身做主键之外，还存一份 text 用于
    事后审计（确认命中的确实是同一段话）。
    """

    def __init__(self, db_path: str = KB_STATE_DB):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS embed_cache (
                text_sha   TEXT PRIMARY KEY,
                text       TEXT NOT NULL,
                model      TEXT NOT NULL,
                dim        INTEGER NOT NULL,
                vector_blob BLOB NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        self._conn.commit()

    def get(self, text_sha: str, model: str, dim: int) -> Optional[List[float]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT vector_blob FROM embed_cache "
                "WHERE text_sha = ? AND model = ? AND dim = ?",
                (text_sha, model, dim),
            ).fetchone()
        if row is None:
            return None
        blob = row[0]
        n = len(blob) // 4
        return list(struct.unpack("<%df" % n, blob))

    def put(self, text_sha: str, text: str, model: str, dim: int,
            vector: List[float]):
        blob = struct.pack("<%df" % len(vector), *vector)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO embed_cache "
                "(text_sha, text, model, dim, vector_blob, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (text_sha, text, model, dim, blob, time.time()),
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM embed_cache").fetchone()[0]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
