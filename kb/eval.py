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


# ======================================================================
# 生成质量评估（离线、需真实 API；不进 promote 门禁）
# ======================================================================
# 召回评估衡量"能不能找到"，生成评估衡量"回答好不好"：
#   1. 引用覆盖率 —— 回答中实际出现引用行的比例
#   2. LLM-as-judge —— 用评审模型对回答的忠实度/完整性/清晰度打分
# 该评估消耗 API 额度，由开发者手动运行：kb eval-gen <build_id>

_JUDGE_PROMPT = """你是一位严格的答案质量评审员。请从三个维度给下面的回答打分（1-5 分，5 为最佳）：

1. 忠实度（faithfulness）：回答是否忠实于检索到的文献内容，没有编造引用、张冠李戴或把自身知识冒充原文；
2. 完整性（completeness）：是否完整回应了问题的各个层面，而不是只答一半；
3. 清晰度（clarity）：结构是否清晰、层次是否分明、表达是否易懂。

问题：{question}
回答：{answer}

只输出 JSON，不要任何其他文字：
{{"faithfulness": 1-5的整数, "completeness": 1-5的整数, "clarity": 1-5的整数, "comment": "一句话点评"}}"""


def evaluate_generation(build_id: str, index_dir: str,
                        top_k: int = TOP_K,
                        judge_model: Optional[str] = None) -> Dict:
    """对 golden 集逐条跑完整生成，输出引用覆盖率与 LLM-as-judge 评分

    参数:
        judge_model: 评审模型 ID；留空用当前对话模型（config 的 model）。
        注意:本函数调用真实 LLM,消耗 API 额度,离线手动运行。
    """
    golden = load_golden()
    if not golden:
        return {"ok": None, "questions": 0, "note": "golden 集为空"}

    from rag.generator import RAGPipeline
    from rag.config_store import get_config

    cfg = get_config()
    pipeline = RAGPipeline(index_dir=index_dir)
    judge = judge_model or cfg.get("model")

    per_question = []
    coverage_hits = 0
    coverage_total = 0
    judge_scores = {"faithfulness": [], "completeness": [], "clarity": []}

    for item in golden:
        q = item["question"]
        answer_parts = []
        try:
            for evt in pipeline.ask_stream(
                question=q, top_k=top_k, fetch_k=int(cfg.get("fetch_k")),
                thinking_effort="off",
            ):
                if evt.get("type") == "token":
                    answer_parts.append(evt.get("content", ""))
        except Exception as e:
            logger.warning("生成评估失败 [%s]: %s", q[:20], e)
            per_question.append({"question": q, "error": str(e)[:100]})
            continue

        answer = "".join(answer_parts)
        import re as _re
        cite_count = len(_re.findall(r'参考自《', answer))

        # LLM-as-judge 评分
        score = {"faithfulness": None, "completeness": None,
                 "clarity": None, "comment": ""}
        try:
            from openai import OpenAI
            client = OpenAI(api_key=cfg.get("api_key"),
                            base_url=cfg.get("api_base_url"), timeout=30)
            resp = client.chat.completions.create(
                model=judge,
                messages=[{"role": "user", "content": _JUDGE_PROMPT.format(
                    question=q, answer=answer[:3000])}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            for k in ("faithfulness", "completeness", "clarity"):
                v = data.get(k)
                if isinstance(v, (int, float)):
                    score[k] = int(v)
                    judge_scores[k].append(int(v))
            score["comment"] = str(data.get("comment", ""))[:100]
        except Exception as e:
            logger.warning("judge 评分失败 [%s]: %s", q[:20], e)

        coverage_hits += 1 if cite_count > 0 else 0
        coverage_total += 1
        per_question.append({
            "question": q, "answer_len": len(answer),
            "cite_lines": cite_count, "judge": score,
        })

    n = len(per_question)
    result = {
        "ok": True,
        "questions": n,
        "coverage": {
            "with_citation": round(coverage_hits / coverage_total, 4)
            if coverage_total else 0.0,
            "avg_cite_lines": round(
                sum(p.get("cite_lines", 0) for p in per_question) / n, 2)
            if n else 0.0,
        },
        "judge": {
            key: (round(sum(vals) / len(vals), 2) if vals else None)
            for key, vals in judge_scores.items()
        },
        "per_question": per_question,
    }
    logger.info("生成评估完成: 引用覆盖率=%.0f%% judge=%s",
                result["coverage"]["with_citation"] * 100,
                result["judge"])
    _write_back(build_id, result)
    return result
