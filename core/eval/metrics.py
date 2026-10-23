"""Deterministic answer, retrieval, calibration, and operations metrics."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .schema import GoldenCase


@dataclass(frozen=True)
class CasePrediction:
    case_id: str
    entitlement: str
    enforcement_posture: Mapping[str, Any]
    cited_clauses: tuple[str, ...] = ()
    refusal: bool = False
    refusal_reason: str | None = None
    claims_refund_not_owed_due_to_posture: bool = False


def evaluate_answers(
    cases: Sequence[GoldenCase], predictions: Mapping[str, CasePrediction]
) -> dict[str, Any]:
    """Score expected answer fields; missing and extra case outputs are errors."""
    if not cases:
        raise ValueError("at least one golden case is required")
    expected_ids = {case.case_id for case in cases}
    if set(predictions) != expected_ids:
        missing = sorted(expected_ids - set(predictions))
        extra = sorted(set(predictions) - expected_ids)
        raise ValueError(
            f"prediction IDs must match golden cases (missing={missing}, extra={extra})"
        )
    if any(predictions[case_id].case_id != case_id for case_id in predictions):
        raise ValueError("prediction mapping keys must match each prediction.case_id")

    family_correct: dict[str, list[bool]] = defaultdict(list)
    posture_correct: list[bool] = []
    refusal_correct: list[bool] = []
    over_refusal: list[bool] = []
    expected_clause_count = predicted_clause_count = matched_clause_count = 0
    conflation_cases: list[str] = []

    for case in cases:
        prediction = predictions[case.case_id]
        expected = case.expected
        family_correct[case.family].append(prediction.entitlement == expected.entitlement)
        expected_posture = expected.enforcement_posture
        posture_match = all(
            prediction.enforcement_posture.get(key) == value
            for key, value in {
                "status": expected_posture.status,
                "regime_id": expected_posture.regime_id,
                "checked": expected_posture.checked,
                "notice_ids": list(expected_posture.notice_ids),
                "obligation_preserved": expected_posture.obligation_preserved,
            }.items()
        )
        posture_correct.append(posture_match)
        refusal_matches = prediction.refusal is expected.expected_refusal
        if expected.expected_refusal and expected.refusal_reason is not None:
            refusal_matches = (
                refusal_matches and prediction.refusal_reason == expected.refusal_reason
            )
        refusal_correct.append(refusal_matches)
        if case.family != "insufficient_facts":
            over_refusal.append(prediction.refusal)

        expected_clauses = set(expected.governing_clauses)
        cited = set(prediction.cited_clauses)
        expected_clause_count += len(expected_clauses)
        predicted_clause_count += len(cited)
        matched_clause_count += len(expected_clauses & cited)

        enforcement_discretion = expected_posture.status == "enforcement_discretion"
        expected_eligible = expected.entitlement == "REFUND_ELIGIBLE"
        if (
            expected_eligible
            and enforcement_discretion
            and (
                prediction.claims_refund_not_owed_due_to_posture
                or prediction.entitlement == "REFUND_NOT_ELIGIBLE"
            )
        ):
            conflation_cases.append(case.case_id)

    family_rates = {
        family: {"correct": sum(values), "total": len(values), "accuracy": _rate(values)}
        for family, values in sorted(family_correct.items())
    }
    total_answers = len(cases)
    return {
        "case_count": total_answers,
        "entitlement_accuracy": {
            "correct": sum(sum(values) for values in family_correct.values()),
            "total": total_answers,
            "overall": sum(sum(values) for values in family_correct.values()) / total_answers,
            "per_family": family_rates,
        },
        "posture_correctness": {
            "correct": sum(posture_correct),
            "total": len(posture_correct),
            "accuracy": _rate(posture_correct),
            "enforcement_posture_family_accuracy": family_rates.get(
                "enforcement_posture", {"correct": 0, "total": 0, "accuracy": None}
            )["accuracy"],
        },
        "obligation_enforcement_conflation": {
            "count": len(conflation_cases),
            "case_ids": conflation_cases,
            "zero_tolerance_pass": not conflation_cases,
        },
        "citation": {
            "true_positives": matched_clause_count,
            "predicted": predicted_clause_count,
            "expected": expected_clause_count,
            "precision": (
                matched_clause_count / predicted_clause_count if predicted_clause_count else 0.0
            ),
            "recall": (
                matched_clause_count / expected_clause_count if expected_clause_count else 0.0
            ),
        },
        "refusal": {
            "correct": sum(refusal_correct),
            "total": len(refusal_correct),
            "accuracy": _rate(refusal_correct),
            "over_refusal_count": sum(over_refusal),
            "over_refusal_total": len(over_refusal),
            "over_refusal_rate": _rate(over_refusal),
        },
    }


def retrieval_metrics(
    cases: Sequence[GoldenCase], ranked_document_ids: Mapping[str, Sequence[str]], *, k: int = 10
) -> dict[str, float | int]:
    """Compute per-query recall@k and reciprocal rank from expected clauses."""
    if k < 1:
        raise ValueError("k must be positive")
    if not cases:
        raise ValueError("at least one golden case is required")
    expected_ids = {case.case_id for case in cases}
    if set(ranked_document_ids) != expected_ids:
        raise ValueError("retrieval result IDs must match the evaluated golden cases")
    recalls: list[float] = []
    recalls_at_5: list[float] = []
    recalls_at_10: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcg_at_10: list[float] = []
    for case in cases:
        relevant = set(case.expected.governing_clauses)
        ranked = list(ranked_document_ids[case.case_id])
        top_k = ranked[:k]
        recalls.append(len(relevant & set(top_k)) / len(relevant) if relevant else 0.0)
        recalls_at_5.append(
            len(relevant & set(ranked[:5])) / len(relevant) if relevant else 0.0
        )
        recalls_at_10.append(
            len(relevant & set(ranked[:10])) / len(relevant) if relevant else 0.0
        )
        first_relevant = next(
            (index for index, value in enumerate(ranked, 1) if value in relevant), None
        )
        reciprocal_ranks.append(1.0 / first_relevant if first_relevant else 0.0)
        dcg = sum(
            1.0 / math.log2(position + 1)
            for position, document_id in enumerate(ranked[:10], 1)
            if document_id in relevant
        )
        ideal_count = min(len(relevant), 10)
        ideal_dcg = sum(1.0 / math.log2(position + 1) for position in range(1, ideal_count + 1))
        ndcg_at_10.append(dcg / ideal_dcg if ideal_dcg else 0.0)
    return {
        "query_count": len(cases),
        "k": k,
        "recall_at_k": sum(recalls) / len(recalls),
        "recall_at_5": sum(recalls_at_5) / len(recalls_at_5),
        "recall_at_10": sum(recalls_at_10) / len(recalls_at_10),
        "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
        "ndcg_at_10": sum(ndcg_at_10) / len(ndcg_at_10),
    }


ABLATION_MODES = ("bm25", "vector", "hybrid", "hybrid_rerank")


def retrieval_ablation(
    cases: Sequence[GoldenCase], inputs_by_mode: Mapping[str, Mapping[str, Sequence[str]]]
) -> dict[str, dict[str, float | int]]:
    """Score supplied rankings for the four required ablations; never fabricates runs."""
    if set(inputs_by_mode) != set(ABLATION_MODES):
        raise ValueError(f"ablation modes must be exactly {list(ABLATION_MODES)}")
    return {mode: retrieval_metrics(cases, inputs_by_mode[mode], k=10) for mode in ABLATION_MODES}


def calibration_metrics(
    probabilities: Sequence[float], labels: Sequence[int], *, bins: int = 10
) -> dict[str, Any]:
    """Brier, ECE, and reliability bins for caller-supplied held-out predictions."""
    if not probabilities or len(probabilities) != len(labels):
        raise ValueError("probabilities and labels must have equal non-zero length")
    if bins < 1 or any(not 0.0 <= p <= 1.0 for p in probabilities):
        raise ValueError("bins must be positive and probabilities within [0, 1]")
    if any(label not in (0, 1) for label in labels):
        raise ValueError("calibration labels must be binary")
    reliability = []
    ece = 0.0
    for index in range(bins):
        lower, upper = index / bins, (index + 1) / bins
        rows = [
            position
            for position, probability in enumerate(probabilities)
            if lower <= probability < upper or (index == bins - 1 and probability == 1.0)
        ]
        mean_prediction = (
            sum(probabilities[position] for position in rows) / len(rows) if rows else None
        )
        observed_rate = sum(labels[position] for position in rows) / len(rows) if rows else None
        if rows and mean_prediction is not None and observed_rate is not None:
            ece += len(rows) / len(labels) * abs(mean_prediction - observed_rate)
        reliability.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(rows),
                "mean_predicted": mean_prediction,
                "observed_rate": observed_rate,
            }
        )
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, labels, strict=True)) / len(labels)
    return {"brier_score": brier, "ece": ece, "reliability_curve": reliability}


def operations_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, float | int | None]:
    """Summarize measured run telemetry; records require no fabricated defaults."""
    if not records:
        raise ValueError("at least one measured operations record is required")
    latency = [float(record["latency_ms"]) for record in records]
    costs = [float(record["cost_usd"]) for record in records]
    tool_errors = [bool(record["tool_error"]) for record in records]
    cache_values = [
        record["cache_hit"] for record in records if record.get("cache_hit") is not None
    ]
    if any(value < 0 for value in latency + costs):
        raise ValueError("latency and cost values must be non-negative")
    ordered_latency = sorted(latency)
    p95_index = max(0, math.ceil(0.95 * len(ordered_latency)) - 1)
    return {
        "query_count": len(records),
        "p95_latency_ms": ordered_latency[p95_index],
        "mean_cost_usd_per_query": sum(costs) / len(costs),
        "tool_error_rate": sum(tool_errors) / len(tool_errors),
        "cache_hit_rate": sum(bool(value) for value in cache_values) / len(cache_values)
        if cache_values
        else None,
        "cache_observation_count": len(cache_values),
    }


def _rate(values: Sequence[bool]) -> float | None:
    return sum(values) / len(values) if values else None
