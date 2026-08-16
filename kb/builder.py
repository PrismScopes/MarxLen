# -*- coding: utf-8 -*-
"""
索引构建器 —— 知识库活水更新的心脏

两种模式：
  full         全量构建：所有语料文件重新分块、嵌入、建索引。
  incremental  增量构建：以某个既有版本为基线，仅重处理
               新增/变更文件的 chunk，未变文件的 chunk 连同向量
               整体复制，嵌入前先查文本级缓存。

安全约束（硬性）：
  - 产物只写入 data/builds/<build_id>/，绝不写入 rag/ 运行目录；
  - 读取基线版本时用 SQLite 只读 URI 打开，不产生 -wal/-shm 文件，
    即"读基线"对基线文件零副作用；
  - 基线是 seed（v1 老库）或分块参数不一致时自动退化为全量，
    绝不把老库的 chunk 与新规则 chunk 混在同一版索引里。
"""

import json
import logging
import os
import sqlite3
import threading
import time
import concurrent.futures
from typing import Dict, List, Optional, Tuple

import faiss
import numpy as np
from rank_bm25 import BM25Okapi

from .cas import build_id_slug, text_sha
from .chunking import chunking_params, process_files
from .manifest import (
    diff_manifests, load_manifest, manifest_root_hash,
    save_manifest, scan_sources,
)
from .paths import (
    build_dir, build_json_path, ensure_dirs, MANIFESTS_DIR, BUILDS_DIR,
    SEED_BUILD_ID, WW_DIR,
)
from .state import PipelineState, TextEmbeddingCache

logger = logging.getLogger(__name__)

EMBED_WORKERS = 3
EMBED_BATCH = 64
SQLITE_RO = "file:%s?mode=ro"
SQLITE_RO_URI = True


# ======================================================================
# 基线读取（只读，对基线文件零副作用）
# ======================================================================

def _load_build_json(build_id: str) -> Optional[Dict]:
    """读取某版本的 build.json；不存在返回 None"""
    path = build_json_path(build_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("build.json 读取失败: %s - %s", path, e)
        return None


def _resolve_base_index_dir(build_id: str) -> Optional[str]:
    """按 build.json 里记录的 index_dir 定位基线三件套所在目录"""
    meta = _load_build_json(build_id)
    if not meta:
        return None
    return meta.get("index_dir")


def _read_base_chunks(index_dir: str, sources: List[str]) -> List[Dict]:
    """从基线 SQLite 只读地取出指定文件的全部 chunk 行

    返回列表元素: {id, uuid, text, title, chapter, category, source}
    用只读 URI 连接，不触发 WAL，不写任何文件。
    """
    db_path = os.path.join(index_dir, "documents.db")
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(SQLITE_RO % db_path, uri=SQLITE_RO_URI)
    try:
        rows = []
        for src in sources:
            cur = conn.execute(
                "SELECT id, uuid, text, title, chapter, category, source "
                "FROM documents WHERE source = ?", (src,))
            for r in cur.fetchall():
                rows.append({
                    "id": r[0], "uuid": r[1], "text": r[2],
                    "title": r[3], "chapter": r[4],
                    "category": r[5], "source": r[6],
                })
        return rows
    finally:
        conn.close()


def _read_base_vectors(index_dir: str, ids: List[int]) -> Dict[int, np.ndarray]:
    """从基线 FAISS 按外部 id 取回原始向量

    Python 绑定里 IndexIDMap.reconstruct(key) 未实现（C 层抛
    not implemented），正确做法是：用 id_map 建「外部 id -> 子索引
    位置」映射，再对子索引（HNSWFlat/Flat）按位置 reconstruct。
    缺失的 id 静默跳过——只要 SQLite 行与 FAISS 向量一一对应
    （构建时已验证过），正常情况全部命中。
    """
    faiss_path = os.path.join(index_dir, "faiss_index.idx")
    if not os.path.exists(faiss_path) or not ids:
        return {}
    index = faiss.read_index(faiss_path)

    out: Dict[int, np.ndarray] = {}
    try:
        id_map = faiss.vector_to_array(index.id_map)
        sub = index.index
        pos_by_id = {int(k): i for i, k in enumerate(id_map)}
        for i in ids:
            p = pos_by_id.get(int(i))
            if p is None:
                continue
            vec = np.zeros(index.d, dtype=np.float32)
            sub.reconstruct(p, vec)
            out[i] = vec
    except Exception as e:
        # 非 IDMap 索引（理论上不会发生），退化为逐 id reconstruct
        logger.warning("按 id_map 取回向量失败，改用逐条 reconstruct: %s", e)
        for i in ids:
            vec = np.zeros(index.d, dtype=np.float32)
            try:
                index.reconstruct(int(i), vec)
                out[i] = vec
            except Exception:
                logger.debug("基线向量缺失: id=%s", i)
    return out


# ======================================================================
# 构建器
# ======================================================================

class IndexBuilder:
    """全量 / 增量索引构建

    用法:
        result = IndexBuilder().build()               # 增量（基线=当前发布版）
        result = IndexBuilder(full=True).build()      # 全量
    """

    def __init__(self, full: bool = False, base_build_id: Optional[str] = None,
                 ww_dir: str = WW_DIR):
        self.full = full
        self.base_build_id = base_build_id
        self.ww_dir = ww_dir

    # ── 主流程 ────────────────────────────────────────────────

    def build(self) -> Dict:
        build_id = build_id_slug()
        out_dir = build_dir(build_id)
        ensure_dirs(BUILDS_DIR, MANIFESTS_DIR)
        os.makedirs(out_dir, exist_ok=True)

        state = PipelineState()
        state.set_step(build_id, "started")
        logger.info("开始构建 %s（输出目录: %s）", build_id, out_dir)

        try:
            # 1. 语料扫描与基线判定
            new_manifest = scan_sources(self.ww_dir)
            save_manifest(new_manifest, build_id)

            base_id, base_index_dir = self._pick_base()
            base_chunks = []          # 待复制的未变 chunk
            copy_sources: List[str] = []

            if base_id is not None and base_index_dir is not None:
                base_manifest = load_manifest(base_id)
                diff = diff_manifests(base_manifest, new_manifest)
                logger.info(
                    "相对基线 %s 的变更: 新增 %d / 删除 %d / 修改 %d / 未变 %d",
                    base_id, len(diff["added"]), len(diff["removed"]),
                    len(diff["changed"]), len(diff["unchanged"]))
                copy_sources = diff["unchanged"]
                base_chunks = _read_base_chunks(base_index_dir, copy_sources)
                changed_files = diff["added"] + diff["changed"]
            else:
                logger.info("无可用基线，按全量构建处理")
                diff = {"added": [], "removed": [], "changed": [],
                        "unchanged": []}
                changed_files = sorted(new_manifest.keys())

            state.set_step(build_id, "diffed",
                           {"base": base_id,
                            "changed_files": len(changed_files)})

            # 2. 分块：只处理变更文件
            logger.info("分块处理 %d 个变更文件...", len(changed_files))
            new_chunks = process_files(self.ww_dir, changed_files)
            logger.info("新分块 %d 个，基线复制 %d 个",
                        len(new_chunks), len(base_chunks))

            # 3. 嵌入新 chunk（文本级缓存 + 并发批次 + 失败降级）
            embedder = None
            embedding_model = ""
            counts = {"copied": 0, "new_chunks": len(new_chunks),
                      "embedded": 0, "embed_cache_hits": 0,
                      "api_batches": 0, "failed": 0}
            if new_chunks:
                from rag.rag_embed import QwenEmbedder  # 延迟导入，仅离线进程需要
                embedder = QwenEmbedder()
                embedding_model = embedder.model
                self._embed_chunks(new_chunks, embedder, counts, build_id)

            # 4. 组装基线的复制集合（向量缺失的降级为重新嵌入）
            copied_rows = self._assemble_copied(
                base_chunks, base_index_dir, counts, embedder, build_id)

            # 5. 写三件套
            from rag.vector_store import FAISSStore  # 延迟导入
            self._write_indexes(
                out_dir, copied_rows, new_chunks, build_id,
                counts, embedder, state)

            # 6. 血缘
            from rag.bm25_tokenizer import tokenize_bm25  # 供 build.json 记录
            del tokenize_bm25
            meta = {
                "build_id": build_id,
                "type": "full" if base_id is None else "incremental",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "base_build_id": base_id,
                "index_dir": out_dir,
                "source_root_hash": manifest_root_hash(new_manifest),
                "files": len(new_manifest),
                "chunking": chunking_params(),
                "embedding": {
                    "model": embedding_model,
                    "dim": 1024,
                    "api_batches": counts["api_batches"],
                    "embed_cache_hits": counts["embed_cache_hits"],
                },
                "counts": counts,
                "verify": None,
                "eval": None,
            }
            _atomic_write_json(build_json_path(build_id), meta)
            state.set_step(build_id, "built",
                           {"chunks": counts["copied"] + len(new_chunks)})

            logger.info("构建完成: %s（chunk 总数 %d，嵌入失败 %d）",
                        build_id, counts["copied"] + len(new_chunks),
                        counts["failed"])
            return meta
        finally:
            state.close()

    # ── 基线判定 ──────────────────────────────────────────────

    def _pick_base(self) -> Tuple[Optional[str], Optional[str]]:
        """确定增量基线 (build_id, index_dir)；不可用时返回 (None, None)"""
        if self.full:
            return None, None

        if self.base_build_id is None:
            from .release import current_build_id  # 延迟导入避免循环
            self.base_build_id = current_build_id()

        base_id = self.base_build_id
        if not base_id:
            return None, None

        # seed 是 v1 老库：chunk 无 CAS 身份、无 file_hash，
        # 无法按文件级 diff 可靠复用，一律退化为全量
        if base_id == SEED_BUILD_ID:
            logger.info("基线是 seed（v1 老库），无法增量复用，按全量构建")
            return None, None

        base_meta = _load_build_json(base_id)
        if not base_meta:
            logger.warning("基线 %s 的 build.json 缺失，按全量构建", base_id)
            return None, None

        # 分块参数变化时，同一文件的 chunk 集合可能完全不同，
        # 复制会把新旧规则的 chunk 混在一起——必须退化为全量
        if base_meta.get("chunking") != chunking_params():
            logger.warning("基线分块参数与当前不一致，按全量构建")
            return None, None

        base_index_dir = base_meta.get("index_dir")
        if not base_index_dir or not os.path.isdir(base_index_dir):
            logger.warning("基线索引目录不存在: %s，按全量构建", base_index_dir)
            return None, None
        if not load_manifest(base_id):
            logger.warning("基线 manifest 缺失，无法做文件级 diff，按全量构建")
            return None, None

        return base_id, base_index_dir

    # ── 嵌入 ──────────────────────────────────────────────────

    def _embed_chunks(self, chunks: List[Dict], embedder,
                      counts: Dict, build_id: str):
        """并发嵌入全部新 chunk；命中文本缓存或失败（跳过）均不阻塞构建"""
        cache = TextEmbeddingCache()
        model = embedder.model
        dim = embedder.get_embedding_dim()
        state = PipelineState()

        # 先查缓存，把未命中的 chunk 分组成批
        pending: List[Tuple[int, str]] = []   # (chunk 下标, text_sha)
        for i, c in enumerate(chunks):
            sha = text_sha(c["text"])
            vec = cache.get(sha, model, dim)
            if vec is not None:
                c["_vector"] = vec
                counts["embed_cache_hits"] += 1
            else:
                pending.append((i, sha))
        logger.info("嵌入: %d 个命中缓存，%d 个待嵌入",
                    counts["embed_cache_hits"], len(pending))

        batches = [
            pending[j:j + EMBED_BATCH]
            for j in range(0, len(pending), EMBED_BATCH)
        ]
        lock = threading.Lock()
        results: Dict[int, List[float]] = {}

        def run_batch(batch):
            idxs = [i for i, _ in batch]
            texts = [chunks[i]["text"] for i, _ in batch]
            try:
                vectors = embedder.embed_batch(texts)
                with lock:
                    counts["api_batches"] += 1
                    for k, i in enumerate(idxs):
                        results[i] = vectors[k]
            except Exception as e:
                logger.warning("嵌入批次失败，逐条重试: %s", str(e)[:100])
                for k, i in enumerate(idxs):
                    try:
                        vec = embedder.embed_single(texts[k])
                        with lock:
                            counts["api_batches"] += 1
                            results[i] = vec
                    except Exception as e2:
                        logger.error("chunk 嵌入失败，跳过: %s", str(e2)[:100])

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=EMBED_WORKERS) as ex:
            list(ex.map(run_batch, batches))

        # 回填结果并写缓存
        for i, sha in pending:
            vec = results.get(i)
            if vec is None:
                counts["failed"] += 1
                continue
            chunks[i]["_vector"] = vec
            counts["embedded"] += 1
            cache.put(sha, chunks[i]["text"], model, dim, vec)

        state.set_step(build_id, "embedded", {
            "embedded": counts["embedded"],
            "cache_hits": counts["embed_cache_hits"],
            "failed": counts["failed"],
        })
        cache.close()
        state.close()

    # ── 复制与组装 ────────────────────────────────────────────

    def _assemble_copied(self, base_chunks: List[Dict],
                         base_index_dir: Optional[str],
                         counts: Dict, embedder,
                         build_id: str) -> List[Dict]:
        """把基线 chunk 连同向量取回；向量缺失的降级为重新嵌入"""
        rows: List[Dict] = []
        if not base_chunks:
            return rows

        ids = [c["id"] for c in base_chunks]
        vectors = _read_base_vectors(base_index_dir, ids)
        missing = [c for c in base_chunks if c["id"] not in vectors]

        if missing and embedder is not None:
            logger.info("基线向量缺失 %d 条，重新嵌入", len(missing))
            self._embed_chunks(missing, embedder, counts, build_id)

        for c in base_chunks:
            vec = vectors.get(c["id"])
            if vec is None and "_vector" in c:
                vec = c["_vector"]
            if vec is None:
                counts["failed"] += 1
                continue
            rows.append({**c, "_vector": vec})
            counts["copied"] += 1
        return rows

    # ── 写三件套 ──────────────────────────────────────────────

    def _write_indexes(self, out_dir: str, copied_rows: List[Dict],
                       new_chunks: List[Dict], build_id: str,
                       counts: Dict, embedder, state: PipelineState):
        """在独立目录产出 documents.db + faiss_index.idx + bm25_index.pkl"""
        from rag.vector_store import FAISSStore

        faiss_path = os.path.join(out_dir, "faiss_index.idx")
        db_path = os.path.join(out_dir, "documents.db")

        store = FAISSStore(faiss_path=faiss_path, db_path=db_path)
        store.load()

        # 1) 复制基线行：显式指定主键 id，保持与基线 FAISS 向量 id 对齐
        if copied_rows:
            store.conn.executemany(
                "INSERT INTO documents (id, uuid, text, title, chapter, "
                "category, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(r["id"], r["uuid"], r["text"], r["title"],
                  r["chapter"], r["category"], r["source"])
                 for r in copied_rows],
            )
            store.conn.commit()
            ids_np = np.array([r["id"] for r in copied_rows], dtype=np.int64)
            vecs_np = np.array(
                [np.asarray(r["_vector"], dtype=np.float32)
                 for r in copied_rows], dtype=np.float32)
            store.index.add_with_ids(vecs_np, ids_np)

        # 2) 新 chunk：自动分配 id
        if new_chunks:
            valid = [c for c in new_chunks if "_vector" in c]
            skipped = len(new_chunks) - len(valid)
            if skipped:
                logger.warning("跳过 %d 个无向量 chunk", skipped)
            if valid:
                store.add(
                    uuids=[c["uuid"] for c in valid],
                    texts=[c["text"] for c in valid],
                    vectors=[normalize_l2(c["_vector"]) for c in valid],
                    metadatas=[c["metadata"] for c in valid],
                    persist=False,
                )

        store.persist()
        logger.info("SQLite + FAISS 已写入: %s", out_dir)

        # 3) BM25：对全部 chunk 文本重建（纯本地，不调用 API）
        rows = store.conn.execute(
            "SELECT id, text, title, chapter, category, source "
            "FROM documents ORDER BY id").fetchall()
        from rag.bm25_tokenizer import tokenize_bm25
        corpus = [r[1] or "" for r in rows]
        tokenized = [tokenize_bm25(t) for t in corpus]
        bm25 = BM25Okapi(tokenized)
        metadata = [{
            "id": r[0], "title": r[2], "chapter": r[3],
            "category": r[4], "source": r[5],
        } for r in rows]

        import pickle
        bm25_path = os.path.join(out_dir, "bm25_index.pkl")
        with open(bm25_path, "wb") as f:
            pickle.dump({"bm25_index": bm25, "metadata": metadata}, f)
        logger.info("BM25 已写入: %s（%d 条）", bm25_path, len(rows))

        store.close()
        state.set_step(build_id, "indexes_written",
                       {"rows": len(rows)})


def normalize_l2(vector) -> List[float]:
    """L2 归一化（与建库端 ingest_philosophy.py 一致）"""
    arr = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm == 0:
        return arr.tolist()
    return (arr / norm).tolist()


def _atomic_write_json(path: str, payload: Dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
