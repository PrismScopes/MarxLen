# -*- coding: utf-8 -*-
"""
seed —— 把现有 v1 三件套登记为知识库基线

seed 只做三件只读/新建的事，绝不改写 rag/ 下的任何文件：
  1. 扫描 ww/ 语料，把当前快照存为 seed 的 manifest
  2. 读 rag/ 三件套的统计信息（数量），写入 seed 的 build.json
     （index_dir 指向 rag/，type=seed）
  3. 写 releases.json，current=seed-v1

之后服务启动时解析指针会得到 (rag/, seed-v1)，行为与改造前
完全一致；后续 kb build 以 seed 之后的 CAS 版为基线做增量。
"""

import json
import logging
import os
import sqlite3
import time

from .manifest import manifest_root_hash, save_manifest, scan_sources
from .paths import (
    LEGACY_INDEX_DIR, MANIFESTS_DIR, RELEASES_PATH, SEED_BUILD_ID,
    WW_DIR, build_json_path,
)

logger = logging.getLogger(__name__)

SQLITE_RO = "file:%s?mode=ro"


def _count_legacy() -> dict:
    """只读统计现有 v1 三件套（用只读 URI，不产生任何写入）"""
    db_path = os.path.join(LEGACY_INDEX_DIR, "documents.db")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"未找到现有文档库: {db_path}")
    conn = sqlite3.connect(SQLITE_RO % db_path, uri=True)
    try:
        doc_count = conn.execute(
            "SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        conn.close()

    faiss_path = os.path.join(LEGACY_INDEX_DIR, "faiss_index.idx")
    bm25_path = os.path.join(LEGACY_INDEX_DIR, "bm25_index.pkl")
    if not os.path.exists(faiss_path) or not os.path.exists(bm25_path):
        raise FileNotFoundError("现有索引文件不完整（faiss/bm25）")
    return {"documents": doc_count}


def seed(force: bool = False, ww_dir: str = WW_DIR) -> dict:
    """登记 seed-v1；已存在且非 force 时直接返回现状

    参数:
        ww_dir: 语料目录（测试时指向临时语料）。
    """
    if not os.path.isdir(ww_dir):
        raise FileNotFoundError(f"语料目录不存在: {ww_dir}")

    meta_path = build_json_path(SEED_BUILD_ID)
    if os.path.exists(meta_path) and not force:
        logger.info("seed 已存在，跳过（--force 可重建登记）")
        return {"ok": True, "build_id": SEED_BUILD_ID, "skipped": True}

    # 1. 语料快照
    manifest = scan_sources(ww_dir)
    save_manifest(manifest, SEED_BUILD_ID)

    # 2. 只读统计 v1
    counts = _count_legacy()

    # 3. 写 seed 的 build.json（在 data/builds/seed-v1/ 下，不碰 rag/）
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    meta = {
        "build_id": SEED_BUILD_ID,
        "type": "seed",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_build_id": None,
        "index_dir": LEGACY_INDEX_DIR,
        "source_root_hash": manifest_root_hash(manifest),
        "files": len(manifest),
        "chunking": None,
        "embedding": None,
        "counts": {
            "copied": counts["documents"],
            "new_chunks": 0,
            "embedded": 0,
            "embed_cache_hits": 0,
            "failed": 0,
        },
        "verify": None,
        "eval": None,
        "note": "v1 历史索引的登记快照；物理文件仍位于 rag/ 目录，"
                "增量构建将以此为语料基线，但 chunk 层面无法复用",
    }
    tmp = meta_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, meta_path)

    # 4. 初始化发布指针
    if not os.path.exists(RELEASES_PATH):
        payload = {"current": SEED_BUILD_ID, "history": [SEED_BUILD_ID]}
        with open(RELEASES_PATH + ".tmp", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(RELEASES_PATH + ".tmp", RELEASES_PATH)

    logger.info("seed 登记完成: %s（%d 个语料文件，%d 条现有文档）",
                SEED_BUILD_ID, len(manifest), counts["documents"])
    return {"ok": True, "build_id": SEED_BUILD_ID,
            "files": len(manifest), "documents": counts["documents"]}
