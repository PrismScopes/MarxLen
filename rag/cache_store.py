"""
SQLite 查询缓存模块

提供两层缓存：
  1. 嵌入向量缓存 - 避免重复调用 Embedding API（省钱、提速）
  2. LLM 回答缓存 - 相同问题直接返回历史回答（省钱、秒回）

每层缓存独立配置最大条目数，超出时自动淘汰最旧的条目。
"""

import os
import time
import json
import struct
import sqlite3
import logging
import threading
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

RAG_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = RAG_DIR + "/"
EMBED_CACHE_PATH = os.path.join(CACHE_DIR, "cache_embeddings.db")
ANSWER_CACHE_PATH = os.path.join(CACHE_DIR, "cache_answers.db")
MAX_EMBED_ENTRIES = 2000
MAX_ANSWER_ENTRIES = 500


class EmbeddingCache:
    """嵌入向量缓存：query -> vector"""

    def __init__(self, db_path: str = EMBED_CACHE_PATH, max_entries: int = MAX_EMBED_ENTRIES):
        self.db_path = db_path
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS embed_cache (
                query_hash TEXT PRIMARY KEY,
                query_text TEXT NOT NULL,
                vector_blob BLOB NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        self._conn.commit()

    def _query_hash(self, text: str) -> str:
        """生成查询文本的哈希值（前 100 字 + MD5 前缀，兼顾精度与性能）"""
        import hashlib
        return hashlib.md5(text.encode('utf-8')[:500]).hexdigest()

    def get(self, query: str) -> Optional[List[float]]:
        """从缓存获取向量。未命中返回 None。"""
        qh = self._query_hash(query)
        with self._lock:
            row = self._conn.execute(
                "SELECT vector_blob FROM embed_cache WHERE query_hash = ? AND query_text = ?",
                (qh, query)
            ).fetchone()
        if row is None:
            return None

        # 反序列化 blob -> float list
        blob = row[0]
        n = len(blob) // 4
        vec = list(struct.unpack(f'<{n}f', blob))
        logger.info(f"[缓存命中] 嵌入向量: '{query[:30]}...'")
        return vec

    def put(self, query: str, vector: List[float]):
        """写入缓存"""
        # 淘汰旧的
        self._evict_if_needed()

        qh = self._query_hash(query)
        blob = struct.pack(f'<{len(vector)}f', *vector)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO embed_cache (query_hash, query_text, vector_blob, created_at) VALUES (?, ?, ?, ?)",
                (qh, query, blob, time.time())
            )
            self._conn.commit()

    def _evict_if_needed(self):
        """超出上限时删除最旧的 10%"""
        with self._lock:
            cnt = self._conn.execute("SELECT COUNT(*) FROM embed_cache").fetchone()[0]
            if cnt > self.max_entries:
                delete_cnt = max(10, int(self.max_entries * 0.1))
                self._conn.execute(
                    "DELETE FROM embed_cache WHERE rowid IN (SELECT rowid FROM embed_cache ORDER BY created_at ASC LIMIT ?)",
                    (delete_cnt,)
                )
                self._conn.commit()
                logger.info(f"嵌入缓存淘汰 {delete_cnt} 条")

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM embed_cache").fetchone()[0]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


class AnswerCache:
    """LLM 回答缓存：query -> answer"""

    def __init__(self, db_path: str = ANSWER_CACHE_PATH, max_entries: int = MAX_ANSWER_ENTRIES):
        self.db_path = db_path
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS answer_cache (
                query_hash TEXT PRIMARY KEY,
                query_text TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        self._conn.commit()

    def _query_hash(self, text: str) -> str:
        import hashlib
        return hashlib.md5(text.encode('utf-8')[:500]).hexdigest()

    def get(self, query: str) -> Optional[str]:
        qh = self._query_hash(query)
        with self._lock:
            row = self._conn.execute(
                "SELECT answer FROM answer_cache WHERE query_hash = ? AND query_text = ?",
                (qh, query)
            ).fetchone()
        if row:
            logger.info(f"[缓存命中] 回答: '{query[:30]}...'")
            return row[0]
        return None

    def put(self, query: str, answer: str):
        self._evict_if_needed()
        qh = self._query_hash(query)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO answer_cache (query_hash, query_text, answer, created_at) VALUES (?, ?, ?, ?)",
                (qh, query, answer, time.time())
            )
            self._conn.commit()

    def _evict_if_needed(self):
        with self._lock:
            cnt = self._conn.execute("SELECT COUNT(*) FROM answer_cache").fetchone()[0]
            if cnt > self.max_entries:
                delete_cnt = max(5, int(self.max_entries * 0.1))
                self._conn.execute(
                    "DELETE FROM answer_cache WHERE rowid IN (SELECT rowid FROM answer_cache ORDER BY created_at ASC LIMIT ?)",
                    (delete_cnt,)
                )
                self._conn.commit()
                logger.info(f"回答缓存淘汰 {delete_cnt} 条")

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM answer_cache").fetchone()[0]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
