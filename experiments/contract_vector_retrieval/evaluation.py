"""不依赖 Elasticsearch 的确定性向量排名与指标计算。"""

from __future__ import annotations

import math
from statistics import fmean
from typing import Mapping, Sequence


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算余弦相似度，并拒绝维度不一致或零向量。"""

    if not left or len(left) != len(right):
        raise ValueError("向量必须非空且维度一致。")
    dot = math.fsum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(math.fsum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(math.fsum(float(value) ** 2 for value in right))
    if left_norm <= 0 or right_norm <= 0:
        raise ValueError("余弦相似度不接受零向量。")
    return dot / (left_norm * right_norm)


def evaluate_query(
    *,
    query_id: str,
    query_kind: str,
    query_text: str | None,
    expected_source_name: str,
    query_vector: Sequence[float],
    candidates: Mapping[str, Sequence[float]],
    candidate_metadata: Mapping[str, dict[str, object]],
) -> dict[str, object]:
    """对一条带唯一目标的查询生成完整候选排名。"""

    if expected_source_name not in candidates:
        raise ValueError(f"目标合同不在候选集中：{expected_source_name}")
    ranked = sorted(
        (
            (source_name, cosine_similarity(query_vector, vector))
            for source_name, vector in candidates.items()
        ),
        key=lambda item: (-item[1], item[0]),
    )
    ranking: list[dict[str, object]] = []
    expected_rank = 0
    expected_score = 0.0
    best_wrong_score: float | None = None
    for rank, (source_name, score) in enumerate(ranked, start=1):
        is_expected = source_name == expected_source_name
        if is_expected:
            expected_rank = rank
            expected_score = score
        elif best_wrong_score is None:
            best_wrong_score = score
        metadata = candidate_metadata[source_name]
        ranking.append(
            {
                "rank": rank,
                "source_name": source_name,
                "document_id": metadata["document_id"],
                "score": round(score, 6),
                "is_expected": is_expected,
            }
        )
    assert expected_rank > 0
    return {
        "query_id": query_id,
        "query_kind": query_kind,
        "query_text": query_text,
        "expected_source_name": expected_source_name,
        "expected_document_id": candidate_metadata[expected_source_name]["document_id"],
        "expected_rank": expected_rank,
        "reciprocal_rank": round(1 / expected_rank, 6),
        "expected_score": round(expected_score, 6),
        "margin_over_best_wrong": (
            round(expected_score - best_wrong_score, 6)
            if best_wrong_score is not None
            else None
        ),
        "ranking": ranking,
    }


def summarize_results(results: Sequence[dict[str, object]]) -> dict[str, object]:
    """汇总唯一目标查询的 Recall@K 与 MRR。"""

    if not results:
        return {
            "query_count": 0,
            "recall_at_1": None,
            "recall_at_3": None,
            "mean_reciprocal_rank": None,
            "mean_expected_score": None,
            "mean_margin_over_best_wrong": None,
        }
    ranks = [int(item["expected_rank"]) for item in results]
    margins = [
        float(item["margin_over_best_wrong"])
        for item in results
        if item["margin_over_best_wrong"] is not None
    ]
    return {
        "query_count": len(results),
        "rank_1_count": sum(rank == 1 for rank in ranks),
        "recall_at_1": round(sum(rank == 1 for rank in ranks) / len(ranks), 6),
        "recall_at_3": round(sum(rank <= 3 for rank in ranks) / len(ranks), 6),
        "mean_reciprocal_rank": round(fmean(1 / rank for rank in ranks), 6),
        "mean_expected_score": round(
            fmean(float(item["expected_score"]) for item in results), 6
        ),
        "mean_margin_over_best_wrong": round(fmean(margins), 6) if margins else None,
    }

