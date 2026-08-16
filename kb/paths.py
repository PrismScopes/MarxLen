# -*- coding: utf-8 -*-
"""
kb 路径常量 —— 数据工程所有落盘位置的唯一定义处

目录布局（全部位于项目根下的 data/，与在线运行目录 rag/ 物理隔离）：

    data/
      sources/               源语料快照（当前版本未使用，预留）
      manifests/<id>.json    每版语料清单（文件级 sha256 快照）
      builds/<id>/           每次构建的独立产物目录
          documents.db       SQLite 文档库
          faiss_index.idx    FAISS 向量索引
          bm25_index.pkl     BM25 关键词索引
          build.json         血缘与构建记录（唯一被发布机制信任的元数据）
      eval/golden.jsonl      质量门禁测试集
      kb_state.db            管线 checkpoint 与文本嵌入缓存
      releases.json          发布指针（原子切换的唯一文件）
"""

import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_ROOT = os.environ.get("KB_DATA_ROOT") or os.path.join(PROJECT_ROOT, "data")
SOURCES_DIR = os.path.join(DATA_ROOT, "sources")
MANIFESTS_DIR = os.path.join(DATA_ROOT, "manifests")
BUILDS_DIR = os.path.join(DATA_ROOT, "builds")
EVAL_DIR = os.path.join(DATA_ROOT, "eval")
GOLDEN_PATH = os.path.join(EVAL_DIR, "golden.jsonl")
RELEASES_PATH = os.path.join(DATA_ROOT, "releases.json")
KB_STATE_DB = os.path.join(DATA_ROOT, "kb_state.db")

# 现有 v1 三件套所在目录（只读，绝不写入）。
# 环境变量覆盖点：KB_DATA_ROOT / KB_LEGACY_INDEX_DIR 供测试把
# 全部落盘与只读源重定向到临时目录，测试永不触碰真实数据。
LEGACY_INDEX_DIR = os.environ.get("KB_LEGACY_INDEX_DIR") \
    or os.path.join(PROJECT_ROOT, "rag")

# 语料目录（只读扫描）
WW_DIR = os.path.join(PROJECT_ROOT, "ww")

# seed 版本号：把现有 v1 索引登记为基线时使用
SEED_BUILD_ID = "seed-v1"


def build_dir(build_id: str) -> str:
    """某版本构建产物的物理目录"""
    return os.path.join(BUILDS_DIR, build_id)


def build_json_path(build_id: str) -> str:
    return os.path.join(build_dir(build_id), "build.json")


def manifest_path(build_id: str) -> str:
    return os.path.join(MANIFESTS_DIR, build_id + ".json")


def ensure_dirs(*dirs: str) -> None:
    """按需创建数据目录（幂等）"""
    for d in dirs:
        os.makedirs(d, exist_ok=True)
