from __future__ import annotations

from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from experiments.contract_visual_retrieval.evaluation import (
    assess_visual_retrieval,
    summarize_visual_retrieval,
)


def test_visual_retrieval_records_rank_and_ranked_candidates() -> None:
    result = assess_visual_retrieval(
        expected_document_id="target",
        expected_source_name="目标合同.pdf",
        visual_page_count=3,
        retrieved_ids=["other", "target", "third"],
        scores=[0.92, 0.81, 0.70],
        source_names=["其他合同.pdf", "目标合同.pdf", "第三合同.pdf"],
    )

    assert result.rank == 2
    assert result.reciprocal_rank == pytest.approx(0.5)
    assert result.candidates[1].is_expected
    assert result.candidates[1].score == pytest.approx(0.81)


def test_visual_retrieval_summary_distinguishes_rank_one_and_found() -> None:
    rank_one = assess_visual_retrieval(
        expected_document_id="one",
        expected_source_name="一.pdf",
        visual_page_count=1,
        retrieved_ids=["one", "two"],
        scores=[1.0, 0.3],
        source_names=["一.pdf", "二.pdf"],
    )
    rank_two = assess_visual_retrieval(
        expected_document_id="two",
        expected_source_name="二.pdf",
        visual_page_count=1,
        retrieved_ids=["one", "two"],
        scores=[0.9, 0.8],
        source_names=["一.pdf", "二.pdf"],
    )
    missed = assess_visual_retrieval(
        expected_document_id="three",
        expected_source_name="三.pdf",
        visual_page_count=1,
        retrieved_ids=["one", "two"],
        scores=[0.9, 0.8],
        source_names=["一.pdf", "二.pdf"],
    )

    summary = summarize_visual_retrieval(
        [rank_one, rank_two, missed], candidate_count=2
    )

    assert summary == {
        "evaluated_count": 3,
        "candidate_count": 2,
        "rank_1_count": 1,
        "rank_1_recall": pytest.approx(1 / 3),
        "recall_at_candidate_count": pytest.approx(2 / 3),
        "mean_reciprocal_rank": pytest.approx(0.5),
    }


def test_visual_retrieval_rejects_duplicate_knn_hits() -> None:
    with pytest.raises(ValueError, match="重复 document_id"):
        assess_visual_retrieval(
            expected_document_id="target",
            expected_source_name="目标合同.pdf",
            visual_page_count=1,
            retrieved_ids=["target", "target"],
            scores=[1.0, 0.9],
            source_names=["目标合同.pdf", "目标合同.pdf"],
        )
