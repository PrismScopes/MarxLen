# -*- coding: utf-8 -*-
"""
一致性验证门禁 —— 不通过就禁止发布

硬门禁（全自动，全过才算通过）：
  1. 三方一致：SQLite 行数 == FAISS 向量数 == BM25 元数据条数
  2. 位置对齐：抽查 BM25 元数据 id 能回查到对应 SQLite 行
     （title/source 一致），保证混合检索不会错位
  3. 定位能力：抽查 chunk 前 N 字能在原文中空白归一化匹配，
     与在线阅读器相同的定位机制（实测基线 99.5%）
  4. 身份唯一：uuid 列无重复（CAS 身份 + 显式复制可能引入冲突）
  5. 嵌入健康：嵌入失败率低于阈值

verify 通过后把结果写回该 build 的 build.json，promote 只认
build.json 里的 verify.ok。
"""

import json
import logging
import os
import pickle
import re
import sqlite3
from typing import Dict, List, Optional

import faiss

from .paths import build_dir, build_json_path, WW_DIR

logger = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
PROBE_LEN = 60
LOCATE_SAMPLE = 50
LOCATE_MIN_RATE = 0.95
MAX_FAILED_RATE = 0.001


def _load_meta(build_id: str) -> Optional[Dict]:
    path = build_json_path(build_id)
    if not os.path.exists(path):
        logger.error("build.json 不存在: %s", path)
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def verify_build(build_id: str, ww_dir: str = WW_DIR) -> Dict:
    """执行全部硬门禁检查，返回 {"ok": bool, "checks": {...}}

    参数:
        ww_dir: 定位抽样使用的语料目录（测试时指向临时语料）。
    """
    meta = _load_meta(build_id)
    if meta is None:
        return {"ok": False, "checks": {"meta": "missing"}}

    index_dir = meta.get("index_dir", build_dir(build_id))
    db_path = os.path.join(index_dir, "documents.db")
    faiss_path = os.path.join(index_dir, "faiss_index.idx")
    bm25_path = os.path.join(index_dir, "bm25_index.pkl")

    checks: Dict = {}
    logger.info("验证构建 %s（索引目录: %s）", build_id, index_dir)

    # ── 1. 三方一致 ─────────────────────────────────────────
    conn = sqlite3.connect(db_path)
    try:
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        index = faiss.read_index(faiss_path)
        vec_count = index.ntotal
        with open(bm25_path, "rb") as f:
            bm25_meta = pickle.load(f)["metadata"]
        bm25_count = len(bm25_meta)
        checks["consistency"] = {
            "sqlite": doc_count, "faiss": vec_count, "bm25": bm25_count,
            "ok": doc_count == vec_count == bm25_count,
        }
        if not checks["consistency"]["ok"]:
            logger.error("三方数量不一致: sqlite=%d faiss=%d bm25=%d",
                         doc_count, vec_count, bm25_count)
            conn.close()
            return {"ok": False, "checks": checks}

        # ── 2. BM25 位置对齐抽查 ─────────────────────────────
        step = max(1, bm25_count // 50)
        mismatched = 0
        checked = 0
        for i in range(0, bm25_count, step):
            m = bm25_meta[i]
            row = conn.execute(
                "SELECT title, source FROM documents WHERE id = ?",
                (m.get("id"),)).fetchone()
            checked += 1
            if row is None or row[0] != m.get("title", "") \
                    or row[1] != m.get("source", ""):
                mismatched += 1
        checks["bm25_alignment"] = {
            "checked": checked, "mismatched": mismatched,
            "ok": mismatched == 0,
        }

        # ── 3. 定位能力抽样 ──────────────────────────────────
        sample = conn.execute(
            "SELECT text, source FROM documents ORDER BY RANDOM() LIMIT ?",
            (LOCATE_SAMPLE,)).fetchall()
        hit = 0
        for text, source in sample:
            if _locate_in_corpus(text, source, ww_dir):
                hit += 1
        rate = hit / len(sample) if sample else 1.0
        checks["locate"] = {
            "sample": len(sample), "hit": hit, "rate": round(rate, 4),
            "ok": rate >= LOCATE_MIN_RATE,
        }

        # ── 4. uuid 唯一性 ───────────────────────────────────
        dup = conn.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT uuid) FROM documents"
        ).fetchone()[0]
        checks["uuid_unique"] = {"duplicates": dup, "ok": dup == 0}

        # ── 5. 嵌入健康 ──────────────────────────────────────
        counts = meta.get("counts", {})
        total = counts.get("copied", 0) + counts.get("new_chunks", 0)
        failed = counts.get("failed", 0)
        failed_rate = failed / total if total else 0.0
        checks["embed_health"] = {
            "total": total, "failed": failed,
            "rate": round(failed_rate, 5),
            "ok": failed_rate <= MAX_FAILED_RATE,
        }
    finally:
        conn.close()

    ok = all(c.get("ok") for c in checks.values())
    result = {"ok": ok, "checks": checks}

    # 写回 build.json，供 promote / 热更新侧查询
    meta["verify"] = result
    tmp = build_json_path(build_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, build_json_path(build_id))

    logger.info("验证结果: %s（定位命中率 %.2f%%）",
                "通过" if ok else "不通过", checks["locate"]["rate"] * 100)
    return result


def _locate_in_corpus(text: str, source: str, ww_dir: str = WW_DIR) -> bool:
    """chunk 前 N 字在原文中的空白归一化匹配（与 api/reader 同机制）"""
    if not text or not source:
        return False
    probe = _WS.sub("", text)[:PROBE_LEN]
    if len(probe) < 10:
        return True  # 太短的样本无法定位，不判失败
    src_path = os.path.join(ww_dir, os.path.basename(source))
    if not os.path.isfile(src_path):
        return False
    try:
        with open(src_path, "r", encoding="utf-8", errors="replace") as f:
            content = _WS.sub("", f.read())
    except OSError:
        return False
    return probe in content
