"""
FAISS + SQLite 向量存储模块

架构：
  - FAISS: 存储 1024 维归一化向量，使用 HNSW 索引实现毫秒级 ANN 搜索
  - SQLite: 存储文档文本和元数据（标题、章节、来源等）
  - FAISS 的 int64 ID 与 SQLite 的 rowid 一一对应，确保检索时能快速回查元数据

相比 Qdrant Local Mode（底层 SQLite + 暴力扫描）的优势：
  - 启动加载 ~2 秒 vs ~60 秒
  - 单次检索 <10ms vs >100ms
  - 不需要额外服务进程
"""

import os
import sqlite3
import logging
import threading
import numpy as np
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# 尝试导入 FAISS，提供友好提示
try:
    import faiss
except ImportError:
    faiss = None
    logger.error(
        "FAISS 未安装。请运行: pip install faiss-cpu\n"
        "如果遇到兼容问题，也可使用下面备用的 NumpyBruteForceStore。"
    )


# ======================================================================
# FAISS + SQLite 向量存储（主力方案）
# ======================================================================

class FAISSStore:
    """FAISS + SQLite 向量存储
    
    用法:
        store = FAISSStore("./faiss_index", "./documents.db")
        store.load()                      # 加载已有索引
        store.add(uuids, texts, vectors, metadatas)  # 批量添加
        results = store.search(query_vec, k=20)       # ANN 检索
    """

    def __init__(self, faiss_path: str = "./faiss_index.idx", db_path: str = "./documents.db"):
        if faiss is None:
            raise ImportError("需要 faiss-cpu 库，请执行: pip install faiss-cpu")

        self.faiss_path = faiss_path
        self.db_path = db_path
        self.dimension = 1024
        self.index: Optional[faiss.Index] = None
        self.conn: Optional[sqlite3.Connection] = None
        # 检索的多个通道会并行读同一个连接。sqlite3.threadsafety==3 只保证
        # 连接对象本身可跨线程使用，并不保证多线程同时 execute 安全——
        # 实测 12 线程并发会抛 InterfaceError: bad parameter or other API
        # misuse。查询很快，用一把锁串起来对耗时几乎无影响。
        self._lock = threading.Lock()

    # ── 初始化 ────────────────────────────────────────────────

    def load(self):
        """加载 FAISS 索引并连接 SQLite。索引不存在时自动创建空索引。"""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

        if os.path.exists(self.faiss_path) and os.path.getsize(self.faiss_path) > 0:
            logger.info(f"正在加载 FAISS 索引: {self.faiss_path}")
            self.index = faiss.read_index(self.faiss_path)
            logger.info(f"FAISS 索引加载完毕，共 {self.index.ntotal} 条向量")
        else:
            logger.info("FAISS 索引文件不存在或为空，将创建新索引")
            self._create_empty_index()
        return self

    def _create_empty_index(self):
        """创建空的 HNSW 索引（内积度量，L2 归一化后等价于余弦相似度）"""
        base_index = faiss.IndexHNSWFlat(self.dimension, 32, faiss.METRIC_INNER_PRODUCT)
        base_index.hnsw.efConstruction = 200
        # 用 IDMap 包装以支持自定义 ID
        self.index = faiss.IndexIDMap(base_index)

    def _init_db(self):
        """初始化 SQLite 表结构"""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid        TEXT    NOT NULL UNIQUE,
                text        TEXT    NOT NULL,
                title       TEXT    DEFAULT '',
                chapter     TEXT    DEFAULT '',
                category    TEXT    DEFAULT '',
                source      TEXT    DEFAULT ''
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_documents_uuid ON documents(uuid)
        """)
        self.conn.commit()

    # ── 写入 ──────────────────────────────────────────────────

    def add(self,
            uuids: List[str],
            texts: List[str],
            vectors: List[List[float]],
            metadatas: List[Dict[str, str]],
            persist: bool = True) -> List[int]:
        """批量添加文档

        参数:
            persist: 是否立即把 FAISS 索引落盘。批量建库时传 False，
                     全部写完后调用一次 persist()，避免每批都全量重写索引文件。

        返回: 本次写入的 SQLite 主键列表（与入参顺序一一对应）。
        调用方需要用它把外部索引（如 BM25）的位置序与 SQLite 行号对齐。
        """
        if not uuids:
            return []

        n = len(uuids)
        if self.index is None or self.index.ntotal == 0:
            self._create_empty_index()

        # 1. 写入 SQLite，获取自增 ID
        rows = [
            (uid, text,
             meta.get("title", ""),
             meta.get("chapter", ""),
             meta.get("category", ""),
             meta.get("source", ""))
            for uid, text, meta in zip(uuids, texts, metadatas)
        ]
        self.conn.executemany(
            "INSERT INTO documents (uuid, text, title, chapter, category, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows
        )
        self.conn.commit()

        # 按 uuid 回查真实主键，而不是依赖 last_insert_rowid() 做算术推算。
        # 回查能保证即使插入不连续（并发写入、AUTOINCREMENT 跳号）也不会错位。
        placeholders = ','.join(['?'] * n)
        id_by_uuid = dict(self.conn.execute(
            f"SELECT uuid, id FROM documents WHERE uuid IN ({placeholders})",
            uuids
        ).fetchall())
        doc_ids = [id_by_uuid[uid] for uid in uuids]

        # 2. 写入 FAISS，用与 SQLite 完全一致的 ID
        vectors_np = np.array(vectors, dtype=np.float32)
        ids_np = np.array(doc_ids, dtype=np.int64)
        self.index.add_with_ids(vectors_np, ids_np)

        # 3. 持久化 FAISS 索引
        if persist:
            faiss.write_index(self.index, self.faiss_path)

        logger.info(f"成功添加 {n} 条文档到向量存储")
        return doc_ids

    def persist(self):
        """将当前 FAISS 索引落盘。配合 add(persist=False) 批量写入后调用一次。"""
        if self.index is not None:
            faiss.write_index(self.index, self.faiss_path)
            logger.info(f"FAISS 索引已保存: {self.faiss_path}（{self.index.ntotal} 条向量）")

    # ── 检索 ──────────────────────────────────────────────────

    def query(self, sql: str, params=()) -> List[tuple]:
        """带锁执行一条只读查询

        检索的多个通道并行读同一个连接，直接用 conn.execute 会抛
        InterfaceError。需要读 documents 表的地方都应走这里。
        """
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def search(self, vector: List[float], k: int = 10) -> List[Dict[str, Any]]:
        """检索最相似的 k 个文档
        
        参数:
            vector: 已归一化的查询向量
            k: 返回数量
            
        返回:
            [{"id", "uuid", "score", "text", "metadata": {...}}, ...]
            id 为 SQLite 主键，与 BM25 元数据中的 id 同属一个空间，
            供上层做跨检索路的结果融合。
        """
        if self.index is None or self.index.ntotal == 0:
            logger.warning("向量索引为空，无法检索")
            return []

        query_np = np.array([vector], dtype=np.float32)
        # FAISS 的 search 不是线程安全的，与 SQLite 一起纳入同一把锁
        with self._lock:
            scores, indices = self.index.search(query_np, k)

            valid = [(float(s), int(i)) for s, i in zip(scores[0], indices[0])
                     if i != -1]
            if not valid:
                return []

            # 一次查回所有命中行，而不是循环里逐条查：
            # k=30 时能省下 29 次往返
            ids = [i for _, i in valid]
            placeholders = ",".join("?" * len(ids))
            rows = {
                r[0]: r for r in self.conn.execute(
                    f"SELECT id, uuid, text, title, chapter, category, source "
                    f"FROM documents WHERE id IN ({placeholders})",
                    ids
                ).fetchall()
            }

        results = []
        # 按 FAISS 返回的相似度顺序输出，不能用 SQL 的返回顺序
        for score, idx in valid:
            row = rows.get(idx)
            if row is None:
                continue
            results.append({
                "id": idx,
                "uuid": row[1],
                "score": score,
                "text": row[2],
                "metadata": {
                    "title": row[3],
                    "chapter": row[4],
                    "category": row[5],
                    "source": row[6],
                }
            })

        return results

    # ── 工具 ──────────────────────────────────────────────────

    def count(self) -> int:
        """返回存储的文档总数"""
        if self.index is not None:
            return self.index.ntotal
        row = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()
        return row[0] if row else 0

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
