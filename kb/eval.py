# -*- coding: utf-8 -*-
"""
golden 集评估 —— 检索质量的软门禁

data/eval/golden.jsonl 每行一条：
    {"question": "问题", "expected_sources": ["文件名1.md"], "difficulty": "easy|hard"}

评估不调用重排 API、不做 LLM 前置解构，直接走
稠密 + 稀疏 → RRF 融合取 top_k，衡量"召回"这一环的原始质量，
稳定且不烧钱。命中判定：结果 source 命中任一 expected_sources。

评估结果写回 build.json 的 eval 字段；promote 时若新版本指标
相对当前发布版下降超过阈值，自动发布被拒绝（除非 --force）。
"""

import json
import logging
import os
from typing import Dict, List, Optional

from .paths import GOLDEN_PATH, build_json_path, EVAL_DIR

logger = logging.getLogger(__name__)

TOP_K = 8


def load_golden() -> List[Dict]:
    """读取 golden 集；文件不存在返回空列表（门禁自动跳过）"""
    if not os.path.exists(GOLDEN_PATH):
        return []
    items = []
    with open(GOLDEN_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
                if obj.get("question") and obj.get("expected_sources"):
                    items.append(obj)
            except json.JSONDecodeError as e:
                logger.warning("golden 行解析失败: %s", e)
    return items


def _hit(results: List[Dict], expected: List[str]) -> int:
    """返回第一条命中的名次（1 起）；未命中返回 0"""
    for rank, r in enumerate(results, 1):
        src = (r.get("metadata", {}).get("source") or "").strip()
        if src in expected:
            return rank
    return 0


def evaluate(build_id: str, index_dir: str, top_k: int = TOP_K) -> Dict:
    """对一个已构建版本跑 golden 评估

    参数:
        index_dir: 该版本三件套所在物理目录（从 build.json 读取）
    """
    golden = load_golden()
    if not golden:
        logger.warning("golden 集为空（%s），评估跳过", GOLDEN_PATH)
        result = {"ok": None, "questions": 0, "note": "golden 集为空"}
        _write_back(build_id, result)
        return result

    from rag.retriever import HybridRetriever

    retriever = HybridRetriever(index_dir=index_dir)

    per_question = []
    reciprocal_ranks = []
    hits = 0
    for item in golden:
        q = item["question"]
        dense = retriever.dense_search(q, k=top_k)
        sparse = retriever.sparse_search(q, k=top_k)
        merged = retriever.rrf_combine(dense, sparse)[:top_k]
        rank = _hit(merged, item["expected_sources"])
        rr = 1.0 / rank if rank else 0.0
        reciprocal_ranks.append(rr)
        hits += 1 if rank else 0
        per_question.append({
            "question": q,
            "hit_rank": rank,
            "top_source": (merged[0].get("metadata", {}).get("source")
                           if merged else None),
        })

    n = len(golden)
    result = {
        "ok": True,
        "questions": n,
        "recall_at_k": round(hits / n, 4),
        "mrr": round(sum(reciprocal_ranks) / n, 4),
        "hit_at_1": round(
            sum(1 for r in reciprocal_ranks if r >= 1.0) / n, 4),
        "per_question": per_question,
    }
    logger.info("评估完成: recall@%d=%.2f%% mrr=%.4f hit@1=%.2f%%",
                top_k, result["recall_at_k"] * 100,
                result["mrr"], result["hit_at_1"] * 100)
    _write_back(build_id, result)
    return result


def load_eval(build_id: str) -> Optional[Dict]:
    """读取某版本已记录的评估结果"""
    path = build_json_path(build_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f).get("eval")


def compare(new_eval: Optional[Dict], old_eval: Optional[Dict],
            max_drop: float = 0.05) -> Dict:
    """新旧评估对比；返回 {"ok", "delta_mrr", ...}

    旧版无评估记录时不作限制（第一次建立指标基线）。
    """
    if not new_eval or new_eval.get("questions", 0) == 0:
        return {"ok": True, "note": "新版本无评估结果，跳过对比"}
    if not old_eval or old_eval.get("questions", 0) == 0:
        return {"ok": True, "note": "基线无评估结果，本次即基线"}

    delta_mrr = new_eval.get("mrr", 0) - old_eval.get("mrr", 0)
    delta_recall = new_eval.get("recall_at_k", 0) - old_eval.get("recall_at_k", 0)
    ok = delta_mrr >= -max_drop
    return {
        "ok": ok,
        "delta_mrr": round(delta_mrr, 4),
        "delta_recall": round(delta_recall, 4),
        "note": "" if ok else (
            f"mrr 下降 {abs(delta_mrr):.2%}，超过允许的 {max_drop:.0%}"),
    }


def _write_back(build_id: str, result: Dict):
    path = build_json_path(build_id)
    if not os.path.exists(path):
        logger.warning("build.json 不存在，评估结果未落盘: %s", path)
        return
    with open(path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    meta["eval"] = result
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
