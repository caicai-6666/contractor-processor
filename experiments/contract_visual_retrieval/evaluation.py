"""视觉自查询召回结果的纯计算与汇总逻辑。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """单次 KNN 查询中一个按分数降序排列的候选。"""

    rank: int
    document_id: str
    source_name: str | None
    score: float
    is_expected: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "document_id": self.document_id,
            "source_name": self.source_name,
            "score": self.score,
            "is_expected": self.is_expected,
        }


@dataclass(frozen=True, slots=True)
class VisualRetrievalResult:
    """一份 PDF 重算视觉向量后的合同级自查询结果。"""

    expected_document_id: str
    expected_source_name: str
    visual_page_count: int
    rank: int | None
    reciprocal_rank: float
    candidates: tuple[RankedCandidate, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected_document_id": self.expected_document_id,
            "expected_source_name": self.expected_source_name,
            "visual_page_count": self.visual_page_count,
            "rank": self.rank,
            "reciprocal_rank": self.reciprocal_rank,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


def assess_visual_retrieval(
    *,
    expected_document_id: str,
    expected_source_name: str,
    visual_page_count: int,
    retrieved_ids: Sequence[str],
    scores: Sequence[float],
    source_names: Sequence[str | None],
) -> VisualRetrievalResult:
    """将 KNN 返回序列转换为可审计的排名结论。"""

    if visual_page_count < 1:
        raise ValueError("视觉页数必须大于 0。")
    if not (len(retrieved_ids) == len(scores) == len(source_names)):
        raise ValueError("KNN 返回的 ID、分数和源文件名数量不一致。")
    if len(set(retrieved_ids)) != len(retrieved_ids):
        raise ValueError("KNN 返回了重复 document_id，无法计算可靠排名。")

    candidates = tuple(
        RankedCandidate(
            rank=rank,
            document_id=document_id,
            source_name=source_name,
            score=float(score),
            is_expected=document_id == expected_document_id,
        )
        for rank, (document_id, score, source_name) in enumerate(
            zip(retrieved_ids, scores, source_names, strict=True), start=1
        )
    )
    rank = next(
        (candidate.rank for candidate in candidates if candidate.is_expected),
        None,
    )
    return VisualRetrievalResult(
        expected_document_id=expected_document_id,
        expected_source_name=expected_source_name,
        visual_page_count=visual_page_count,
        rank=rank,
        reciprocal_rank=0.0 if rank is None else 1.0 / rank,
        candidates=candidates,
    )


def summarize_visual_retrieval(
    results: Sequence[VisualRetrievalResult], *, candidate_count: int
) -> dict[str, Any]:
    """汇总当前候选集上的 Recall@K 与 MRR，不把未执行项目算作失败。"""

    if candidate_count < 1:
        raise ValueError("候选合同数必须大于 0。")
    if not results:
        return {
            "evaluated_count": 0,
            "candidate_count": candidate_count,
            "rank_1_count": 0,
            "rank_1_recall": 0.0,
            "recall_at_candidate_count": 0.0,
            "mean_reciprocal_rank": 0.0,
        }
    ranks = [result.rank for result in results]
    rank_1_count = sum(rank == 1 for rank in ranks)
    found_count = sum(rank is not None for rank in ranks)
    return {
        "evaluated_count": len(results),
        "candidate_count": candidate_count,
        "rank_1_count": rank_1_count,
        "rank_1_recall": rank_1_count / len(results),
        "recall_at_candidate_count": found_count / len(results),
        "mean_reciprocal_rank": sum(
            result.reciprocal_rank for result in results
        )
        / len(results),
    }
