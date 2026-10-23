"""Rubric utility scoring and repeat-judge variance checks."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

UTILITY_WEIGHTS = {
    "factual_accuracy": 0.40,
    "citation_support": 0.30,
    "completeness": 0.20,
    "clarity": 0.10,
}


def utility_score(dimensions: Mapping[str, float]) -> float:
    """Combine four rubric dimensions on [0, 1] with explicit fixed weights."""
    if set(dimensions) != set(UTILITY_WEIGHTS):
        raise ValueError(f"judge dimensions must be exactly {sorted(UTILITY_WEIGHTS)}")
    if any(not 0.0 <= value <= 1.0 for value in dimensions.values()):
        raise ValueError("judge dimension scores must be within [0, 1]")
    return sum(UTILITY_WEIGHTS[name] * dimensions[name] for name in UTILITY_WEIGHTS)


def judge_variance(
    first_pass: Mapping[str, float],
    second_pass: Mapping[str, float],
    *,
    expected_cases: int = 20,
    material_difference: float = 0.02,
) -> dict[str, Any]:
    """Compare the same held-out cases scored twice by the same judge and rubric."""
    if expected_cases < 1 or not 0 <= material_difference <= 1:
        raise ValueError("expected_cases must be positive and material_difference within [0, 1]")
    if set(first_pass) != set(second_pass) or len(first_pass) != expected_cases:
        raise ValueError(f"both judge passes must contain the same {expected_cases} case IDs")
    scores = list(first_pass.values()) + list(second_pass.values())
    if any(not 0.0 <= value <= 1.0 for value in scores):
        raise ValueError("utility scores must be within [0, 1]")
    differences = sorted(abs(first_pass[case_id] - second_pass[case_id]) for case_id in first_pass)
    p95 = differences[max(0, math.ceil(0.95 * len(differences)) - 1)]
    return {
        "status": "measured",
        "case_count": len(differences),
        "mean_absolute_difference": sum(differences) / len(differences),
        "p95_absolute_difference": p95,
        "maximum_absolute_difference": differences[-1],
        "material_disagreements": sum(value > material_difference for value in differences),
        "material_difference_threshold": material_difference,
        "same_judge_and_rubric_required": True,
    }


def cold_rederivation_result(
    expected: Mapping[str, Mapping[str, Any]],
    rederived: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    required_cases: int = 20,
) -> dict[str, Any]:
    """Score completed blind reviews; absent reviews remain pending, never imputed."""
    if len(expected) != required_cases:
        raise ValueError(f"expected exactly {required_cases} selected case IDs")
    if rederived is None or not rederived:
        return {
            "status": "pending_human_review",
            "reviewed_case_count": 0,
            "required_case_count": required_cases,
            "disagreement_count": None,
            "disagreement_rate": None,
            "disagreement_case_ids": [],
            "review_mode": "blind_cold_rederivation",
        }
    if not set(rederived).issubset(expected):
        raise ValueError("cold review returned IDs outside the selected case set")
    fields = ("entitlement", "enforcement_posture", "governing_clauses", "expected_refusal")
    disagreements: list[str] = []
    for case_id, derived in rederived.items():
        gold = expected[case_id]
        if any(derived.get(field) != gold.get(field) for field in fields):
            disagreements.append(case_id)
    return {
        "status": "completed" if len(rederived) == required_cases else "pending_human_review",
        "reviewed_case_count": len(rederived),
        "required_case_count": required_cases,
        "disagreement_count": len(disagreements),
        "disagreement_rate": len(disagreements) / len(rederived) if rederived else None,
        "disagreement_case_ids": disagreements,
        "review_mode": "blind_cold_rederivation",
    }
