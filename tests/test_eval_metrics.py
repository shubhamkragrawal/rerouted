import math
from pathlib import Path

import pytest

from core.eval import (
    calibration_metrics,
    cold_rederivation_result,
    evaluate_answers,
    evaluate_ci,
    judge_variance,
    load_golden_set,
    operations_metrics,
    retrieval_ablation,
    retrieval_metrics,
    utility_score,
)
from core.eval.metrics import CasePrediction

GOLDEN = Path(__file__).parents[1] / "domains" / "air_us" / "golden_set.jsonl"


def test_answer_metrics_separate_over_refusal_and_conflation() -> None:
    cases = load_golden_set(GOLDEN)
    predictions = {
        case.case_id: CasePrediction(
            case_id=case.case_id,
            entitlement=case.expected.entitlement,
            enforcement_posture={
                "status": case.expected.enforcement_posture.status,
                "regime_id": case.expected.enforcement_posture.regime_id,
                "checked": case.expected.enforcement_posture.checked,
                "notice_ids": list(case.expected.enforcement_posture.notice_ids),
                "obligation_preserved": case.expected.enforcement_posture.obligation_preserved,
            },
            cited_clauses=case.expected.governing_clauses,
            refusal=case.expected.expected_refusal,
            refusal_reason=case.expected.refusal_reason,
        )
        for case in cases
    }
    no_over_refusal = evaluate_answers(cases, predictions)
    assert no_over_refusal["refusal"]["over_refusal_rate"] == 0.0

    carveout_case = next(case for case in cases if case.case_id == "gs-052")
    predictions[carveout_case.case_id] = CasePrediction(
        case_id=carveout_case.case_id,
        entitlement="REFUND_NOT_ELIGIBLE",
        enforcement_posture={"status": "enforcement_discretion"},
        cited_clauses=(),
        claims_refund_not_owed_due_to_posture=True,
    )
    score = evaluate_answers(cases, predictions)
    assert score["obligation_enforcement_conflation"]["count"] == 1
    assert score["obligation_enforcement_conflation"]["zero_tolerance_pass"] is False
    assert score["citation"]["recall"] < 1.0


def test_retrieval_metrics_and_four_way_ablation_use_supplied_rankings() -> None:
    cases = load_golden_set(GOLDEN)[:2]
    rankings = {
        cases[0].case_id: [cases[0].expected.governing_clauses[0]],
        cases[1].case_id: ["wrong-clause", cases[1].expected.governing_clauses[0]],
    }
    score = retrieval_metrics(cases, rankings, k=10)
    assert score["recall_at_k"] == pytest.approx(1 / 3)
    assert score["recall_at_5"] == pytest.approx(1 / 3)
    assert score["recall_at_10"] == pytest.approx(1 / 3)
    assert score["mrr"] == pytest.approx(0.75)
    ideal = [
        sum(
            1 / math.log2(position + 1)
            for position in range(1, min(len(case.expected.governing_clauses), 10) + 1)
        )
        for case in cases
    ]
    expected_ndcg = (1 / ideal[0] + (1 / math.log2(3)) / ideal[1]) / 2
    assert score["ndcg_at_10"] == pytest.approx(expected_ndcg)
    ablation_modes = ("bm25", "vector", "hybrid", "hybrid_rerank")
    ablation = retrieval_ablation(cases, {mode: rankings for mode in ablation_modes})
    assert set(ablation) == {"bm25", "vector", "hybrid", "hybrid_rerank"}
    with pytest.raises(ValueError, match="exactly"):
        retrieval_ablation(cases, {"hybrid": rankings})


def test_calibration_metrics_return_brier_ece_and_reliability_bins() -> None:
    score = calibration_metrics([0.1, 0.8], [0, 1], bins=5)
    assert score["brier_score"] == pytest.approx(0.025)
    assert score["ece"] == pytest.approx(0.15)
    assert sum(item["count"] for item in score["reliability_curve"]) == 2


def test_operations_metrics_are_computed_only_from_measured_records() -> None:
    measured = operations_metrics(
        [
            {"latency_ms": 20, "cost_usd": 0.01, "tool_error": False, "cache_hit": True},
            {"latency_ms": 100, "cost_usd": 0.03, "tool_error": True, "cache_hit": False},
            {"latency_ms": 50, "cost_usd": 0.02, "tool_error": False, "cache_hit": None},
        ]
    )
    assert measured["p95_latency_ms"] == 100
    assert measured["mean_cost_usd_per_query"] == pytest.approx(0.02)
    assert measured["tool_error_rate"] == pytest.approx(1 / 3)
    assert measured["cache_hit_rate"] == 0.5
    assert measured["cache_observation_count"] == 2
    with pytest.raises(ValueError, match="measured"):
        operations_metrics([])


def test_judge_utility_and_repeat_variance_require_same_twenty_cases() -> None:
    score = utility_score(
        {"factual_accuracy": 1, "citation_support": 0.5, "completeness": 1, "clarity": 0.5}
    )
    assert score == pytest.approx(0.8)
    first = {f"gs-{index:03d}": 0.9 for index in range(1, 21)}
    second = {**first, "gs-020": 0.85}
    result = judge_variance(first, second)
    assert result["case_count"] == 20
    assert result["mean_absolute_difference"] == pytest.approx(0.0025)
    assert result["material_disagreements"] == 1
    with pytest.raises(ValueError, match="same 20"):
        judge_variance({"gs-001": 0.5}, {"gs-001": 0.5})


def test_cold_rederivation_is_pending_until_human_results_are_supplied() -> None:
    expected = {
        f"gs-{index:03d}": {
            "entitlement": "REFUND_ELIGIBLE",
            "enforcement_posture": {"status": "enforcement_active"},
            "governing_clauses": ["14CFR260.6(a)"],
            "expected_refusal": False,
        }
        for index in range(1, 21)
    }
    pending = cold_rederivation_result(expected)
    assert pending["status"] == "pending_human_review"
    assert pending["disagreement_rate"] is None
    completed = cold_rederivation_result(expected, expected)
    assert completed["status"] == "completed"
    assert completed["disagreement_count"] == 0


def test_ci_reports_pending_baseline_and_hard_current_violations() -> None:
    cases = load_golden_set(GOLDEN)
    predictions = {
        case.case_id: CasePrediction(
            case_id=case.case_id,
            entitlement=case.expected.entitlement,
            enforcement_posture={
                "status": case.expected.enforcement_posture.status,
                "regime_id": case.expected.enforcement_posture.regime_id,
                "checked": case.expected.enforcement_posture.checked,
                "notice_ids": list(case.expected.enforcement_posture.notice_ids),
                "obligation_preserved": case.expected.enforcement_posture.obligation_preserved,
            },
            cited_clauses=case.expected.governing_clauses,
            refusal=case.expected.expected_refusal,
            refusal_reason=case.expected.refusal_reason,
        )
        for case in cases
    }
    score = evaluate_answers(cases, predictions)
    report = evaluate_ci(score)
    assert report["status"] == "pending_baseline_or_measurements"
    assert report["failures"] == []
    assert "retrieval_ablation_requires_measured_rankings" in report["pending"]
    assert "judge_variance_requires_repeat_scoring" in report["pending"]
    assert "cold_rederivation_requires_human_review" in report["pending"]
    bad = {**score, "citation": {**score["citation"], "precision": 0.89}}
    assert "citation_precision_below_0.90" in evaluate_ci(bad)["failures"]

    baseline = {
        **score,
        "operations": {"mean_cost_usd_per_query": 1.0},
    }
    regressed = {
        **baseline,
        "entitlement_accuracy": {
            **score["entitlement_accuracy"],
            "per_family": {
                **score["entitlement_accuracy"]["per_family"],
                "domestic_cancel_controllable": {
                    "accuracy": 0.97,
                    "correct": 9.7,
                    "total": 10,
                },
            },
        },
        "operations": {"mean_cost_usd_per_query": 1.26},
    }
    failures = evaluate_ci(regressed, baseline)["failures"]
    assert "entitlement_accuracy_regressed:domestic_cancel_controllable" in failures
    assert "mean_cost_increased_by_more_than_25_percent" in failures
