# -*- coding: utf-8 -*-
"""
发布管理 —— 原子指针切换

data/releases.json 是整个体系里唯一被"在线端"信任的文件：

    {"current": "b-...", "history": ["b-...", "seed-v1"]}

发布（promote）不移动、不覆盖任何索引文件，只原子替换这个指针。
回滚同理。旧版本目录物理保留，直到用户显式执行 kb gc。

注意：本模块只依赖标准库与 kb 内的纯数据模块，
在线端（rag/generator.py、api/main.py）可直接 import，无循环依赖。
"""

import json
import logging
import os
import shutil
from typing import Dict, List, Optional, Tuple

from .paths import (
    BUILDS_DIR, LEGACY_INDEX_DIR, RELEASES_PATH, SEED_BUILD_ID,
    build_json_path,
)

logger = logging.getLogger(__name__)

KEEP_DEFAULT = 3
MAX_DROP = 0.05


# ======================================================================
# 指针读写
# ======================================================================

def load_releases() -> Optional[Dict]:
    """读取发布指针；文件不存在或损坏返回 None"""
    if not os.path.exists(RELEASES_PATH):
        return None
    try:
        with open(RELEASES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("current"):
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("releases.json 读取失败: %s", e)
    return None


def current_build_id() -> Optional[str]:
    releases = load_releases()
    return releases["current"] if releases else None


def history_build_ids() -> List[str]:
    releases = load_releases()
    return releases.get("history", []) if releases else []


def _atomic_write_releases(payload: Dict) -> None:
    os.makedirs(os.path.dirname(RELEASES_PATH), exist_ok=True)
    tmp = RELEASES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RELEASES_PATH)


# ======================================================================
# 指针解析（在线端唯一入口）
# ======================================================================

def resolve_index_dir() -> Tuple[Optional[str], Optional[str]]:
    """把发布指针解析为三件套物理目录

    返回 (index_dir, build_id)。指针不存在、指向的版本目录缺失时
    返回 (None, None)，调用方应回退到 rag/ 传统目录（v1 行为）。
    """
    build_id = current_build_id()
    if not build_id:
        return None, None

    if build_id == SEED_BUILD_ID:
        return LEGACY_INDEX_DIR, SEED_BUILD_ID

    meta = _load_meta(build_id)
    if not meta:
        logger.warning("当前版本 %s 的 build.json 缺失，回退传统目录", build_id)
        return None, None

    index_dir = meta.get("index_dir")
    if not index_dir or not os.path.isdir(index_dir):
        logger.warning("当前版本索引目录不存在: %s，回退传统目录", index_dir)
        return None, None

    required = ("documents.db", "faiss_index.idx", "bm25_index.pkl")
    if any(not os.path.exists(os.path.join(index_dir, f)) for f in required):
        logger.warning("当前版本索引文件不完整: %s，回退传统目录", index_dir)
        return None, None

    return index_dir, build_id


def _load_meta(build_id: str) -> Optional[Dict]:
    path = build_json_path(build_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ======================================================================
# 发布 / 回滚 / 清理
# ======================================================================

def promote(build_id: str, force: bool = False,
            max_drop: float = MAX_DROP) -> Dict:
    """把某版本切为当前发布版

    门禁（force 可跳过对比门禁，但一致性门禁永远生效）：
      1. build.json 存在且 verify.ok == True（硬门禁）
      2. golden 评估相对当前版无显著下降（软门禁）
    """
    meta = _load_meta(build_id)
    if meta is None:
        return {"ok": False, "reason": f"版本 {build_id} 不存在"}

    verify = meta.get("verify")
    if not verify or not verify.get("ok"):
        return {"ok": False,
                "reason": "一致性验证未通过或未执行，拒绝发布"}

    old_id = current_build_id()
    if not force:
        from .eval import compare, load_eval
        cmp_result = compare(load_eval(build_id), load_eval(old_id)
                             if old_id else None, max_drop=max_drop)
        if not cmp_result.get("ok"):
            return {"ok": False,
                    "reason": cmp_result.get("note", "评估对比未通过")}

    releases = load_releases() or {"current": None, "history": []}
    history = releases.get("history", [])
    new_history = [build_id] + [h for h in history if h != build_id]

    payload = {"current": build_id, "history": new_history}
    _atomic_write_releases(payload)
    logger.info("已发布 %s（历史保留 %d 版）", build_id, len(new_history))
    return {"ok": True, "current": build_id, "history": new_history}


def rollback() -> Dict:
    """回滚到上一发布版本；无可回退版本时返回失败"""
    releases = load_releases()
    if not releases:
        return {"ok": False, "reason": "尚无发布记录"}
    history = releases.get("history", [])
    if len(history) < 2:
        return {"ok": False, "reason": "没有可回退的历史版本"}

    prev = history[1]
    payload = {"current": prev,
               "history": [prev] + [h for h in history if h != prev]}
    _atomic_write_releases(payload)
    logger.info("已回滚到 %s", prev)
    return {"ok": True, "current": prev}


def gc(keep: int = KEEP_DEFAULT, dry_run: bool = True) -> Dict:
    """清理 data/builds 下不在保留列表中的构建目录

    保留集合 = 当前版本 + 历史前 keep 个。绝不删除 seed 对应的
    任何文件（seed 的物理文件在 rag/ 下，本就不在清理范围内）。
    """
    releases = load_releases()
    keep_ids = set()
    if releases:
        keep_ids.add(releases.get("current"))
        keep_ids.update(releases.get("history", [])[:keep])

    removed: List[str] = []
    skipped: List[str] = []
    if os.path.isdir(BUILDS_DIR):
        for name in sorted(os.listdir(BUILDS_DIR)):
            if name in keep_ids:
                skipped.append(name)
                continue
            target = os.path.join(BUILDS_DIR, name)
            if not os.path.isdir(target):
                continue
            if dry_run:
                removed.append(name)
            else:
                shutil.rmtree(target, ignore_errors=True)
                removed.append(name)
    logger.info("gc(%s): 将删除 %d 个构建目录，保留 %d 个",
                "dry-run" if dry_run else "执行", len(removed), len(skipped))
    return {"ok": True, "dry_run": dry_run,
            "removed": removed, "kept": skipped}
