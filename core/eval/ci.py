"""Threshold evaluation with explicit missing-baseline states."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ENTITLEMENT_DROP_TOLERANCE = 0.02
CITATION_PRECISION_MINIMUM = 0.90
COST_INCREASE_TOLERANCE = 0.25


def evaluate_ci(
    current: Mapping[str, Any], baseline: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Apply measurable gates; baseline-dependent gates stay pending until baseline exists."""
    failures: list[str] = []
    pending: list[str] = []
    if not current.get("retrieval_ablation"):
        pending.append("retrieval_ablation_requires_measured_rankings")
    judge_result = current.get("judge_variance")
    if not judge_result or judge_result.get("status") != "measured":
        pending.append("judge_variance_requires_repeat_scoring")
    elif not current.get("judge_variance_reviewed", False):
        pending.append("judge_variance_requires_human_review")
    cold_result = current.get("cold_rederivation")
    if not cold_result or cold_result.get("status") != "completed":
        pending.append("cold_rederivation_requires_human_review")
    elif not current.get("cold_rederivation_reviewed", False):
        pending.append("cold_rederivation_requires_human_approval")
    citation_precision = current.get("citation", {}).get("precision")
    if citation_precision is not None and citation_precision < CITATION_PRECISION_MINIMUM:
        failures.append("citation_precision_below_0.90")
    conflation = current.get("obligation_enforcement_conflation", {}).get("count")
    if conflation is not None and conflation > 0:
        failures.append("obligation_enforcement_conflation_nonzero")
    if current.get("refusal", {}).get("over_refusal_count", 0) > 0:
        failures.append("over_refusal_detected")

    if baseline is None:
        pending.extend(
            [
                "per_family_entitlement_regression_requires_approved_baseline",
                "posture_regression_requires_approved_baseline",
                "refusal_regression_requires_approved_baseline",
                "cost_increase_requires_approved_baseline",
            ]
        )
    else:
        current_families = current.get("entitlement_accuracy", {}).get("per_family", {})
        baseline_families = baseline.get("entitlement_accuracy", {}).get("per_family", {})
        for family, current_result in current_families.items():
            previous = baseline_families.get(family)
            if previous is None:
                pending.append(f"missing_baseline_family:{family}")
            elif (
                previous["accuracy"] - current_result["accuracy"]
                > ENTITLEMENT_DROP_TOLERANCE + 1e-12
            ):
                failures.append(f"entitlement_accuracy_regressed:{family}")
        if current.get("posture_correctness", {}).get("accuracy", 0) < baseline.get(
            "posture_correctness", {}
        ).get("accuracy", 0):
            failures.append("posture_correctness_regressed")
        if current.get("refusal", {}).get("accuracy", 0) < baseline.get("refusal", {}).get(
            "accuracy", 0
        ):
            failures.append("refusal_correctness_regressed")
        current_cost = current.get("operations", {}).get("mean_cost_usd_per_query")
        baseline_cost = baseline.get("operations", {}).get("mean_cost_usd_per_query")
        if current_cost is None or baseline_cost is None:
            pending.append("cost_increase_requires_measured_current_and_baseline_cost")
        elif baseline_cost > 0 and current_cost > baseline_cost * (1 + COST_INCREASE_TOLERANCE):
            failures.append("mean_cost_increased_by_more_than_25_percent")

    if failures:
        status = "fail"
    elif pending:
        status = "pending_baseline_or_measurements"
    else:
        status = "pass"
    return {
        "status": status,
        "failures": failures,
        "pending": pending,
        "citation_precision_minimum": CITATION_PRECISION_MINIMUM,
        "per_family_accuracy_drop_tolerance": ENTITLEMENT_DROP_TOLERANCE,
        "mean_cost_increase_tolerance": COST_INCREASE_TOLERANCE,
        "judge_variance_gate": "pending_human_review; do not gate on an unmeasured judge",
    }
