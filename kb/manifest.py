# -*- coding: utf-8 -*-
"""
语料扫描与 manifest diff

manifest 是一份「文件级」快照：{ 文件名: {sha256, size, mtime_ns} }。
两份快照做差即可得出 added / removed / changed 三类变更，
这是增量构建的入口判断。文件哈希聚合出 root_hash，
任何一个字节变动都会改变它。
"""

import hashlib
import json
import logging
import os
import re
from typing import Dict, List, Optional

from .paths import WW_DIR, MANIFESTS_DIR, manifest_path

logger = logging.getLogger(__name__)

# 与 v1 建库规则一致：文件名带 "(1).md" 的是重复副本，跳过
_DUPLICATE_PATTERN = re.compile(r"\(1\)\.md$")

_HASH_CHUNK = 1024 * 1024


def sha256_file(path: str) -> str:
    """文件内容的 sha256（流式计算，大文件不整读入内存）"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(_HASH_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def scan_sources(ww_dir: str = WW_DIR) -> Dict[str, Dict]:
    """扫描语料目录，返回文件级快照

    只读操作，不写任何文件。目录不存在时返回空快照
    （与在线端"语料缺失只影响阅读器"的降级哲学一致）。
    """
    manifest: Dict[str, Dict] = {}
    if not os.path.isdir(ww_dir):
        logger.warning("语料目录不存在: %s（返回空快照）", ww_dir)
        return manifest

    for name in sorted(os.listdir(ww_dir)):
        if not name.endswith(".md") or _DUPLICATE_PATTERN.search(name):
            continue
        full = os.path.join(ww_dir, name)
        try:
            st = os.stat(full)
            manifest[name] = {
                "sha256": sha256_file(full),
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
            }
        except OSError as e:
            logger.warning("扫描文件失败，跳过: %s - %s", name, e)
    return manifest


def manifest_root_hash(manifest: Dict[str, Dict]) -> str:
    """把文件级快照聚合成一个根哈希（Merkle 根）"""
    h = hashlib.sha256()
    for name in sorted(manifest):
        h.update(name.encode("utf-8"))
        h.update(manifest[name]["sha256"].encode("utf-8"))
    return h.hexdigest()


def save_manifest(manifest: Dict[str, Dict], build_id: str) -> str:
    """把快照落盘到 manifests/<id>.json，返回文件路径"""
    os.makedirs(MANIFESTS_DIR, exist_ok=True)
    payload = {
        "build_id": build_id,
        "root_hash": manifest_root_hash(manifest),
        "files": manifest,
    }
    path = manifest_path(build_id)
    _atomic_write_json(path, payload)
    logger.info("manifest 已保存: %s（%d 个文件）", path, len(manifest))
    return path


def load_manifest(build_id: str) -> Dict[str, Dict]:
    """读取某版本的语料快照；不存在或损坏时返回空字典"""
    path = manifest_path(build_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        files = data.get("files", {})
        return files if isinstance(files, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("manifest 读取失败: %s - %s", path, e)
        return {}


def diff_manifests(old: Dict[str, Dict], new: Dict[str, Dict]) -> Dict[str, List[str]]:
    """两份快照做差，返回 added / removed / changed / unchanged 文件名列表

    changed 的判定只看内容哈希：mtime 变化但内容不变的文件
    归入 unchanged（避免无意义的重新嵌入）。
    """
    old_names = set(old)
    new_names = set(new)

    added = sorted(new_names - old_names)
    removed = sorted(old_names - new_names)
    changed = sorted(
        n for n in (old_names & new_names)
        if old[n].get("sha256") != new[n].get("sha256")
    )
    unchanged = sorted(
        n for n in (old_names & new_names)
        if old[n].get("sha256") == new[n].get("sha256")
    )
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
    }


def _atomic_write_json(path: str, payload: Dict) -> None:
    """先写临时文件再替换，中断不会留下半个文件"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
