"""Calibration metrics and reliability-curve data structures."""

from __future__ import annotations

from typing import TypedDict


class ReliabilityBin(TypedDict):
    lower: float
    upper: float
    mean_predicted: float | None
    observed_rate: float | None
    count: int


def brier_score(probabilities: list[float], labels: list[int]) -> float:
    _aligned(probabilities, labels)
    return sum(
        (probability - label) ** 2 for probability, label in zip(probabilities, labels, strict=True)
    ) / len(labels)


def expected_calibration_error(
    probabilities: list[float], labels: list[int], bins: int = 10
) -> float:
    curve = reliability_curve(probabilities, labels, bins)
    return sum(
        item["count"] / len(labels) * abs(item["mean_predicted"] - item["observed_rate"])
        for item in curve
        if item["mean_predicted"] is not None and item["observed_rate"] is not None
    )


def reliability_curve(
    probabilities: list[float], labels: list[int], bins: int = 10
) -> list[ReliabilityBin]:
    _aligned(probabilities, labels)
    if bins < 1:
        raise ValueError("bins must be positive")
    result: list[ReliabilityBin] = []
    for index in range(bins):
        lower, upper = index / bins, (index + 1) / bins
        indices = [
            row_index
            for row_index, probability in enumerate(probabilities)
            if lower <= probability < upper or (index == bins - 1 and probability == 1.0)
        ]
        count = len(indices)
        result.append(
            {
                "lower": lower,
                "upper": upper,
                "mean_predicted": sum(probabilities[i] for i in indices) / count if count else None,
                "observed_rate": sum(labels[i] for i in indices) / count if count else None,
                "count": count,
            }
        )
    return result


def _aligned(probabilities: list[float], labels: list[int]) -> None:
    if not probabilities or len(probabilities) != len(labels):
        raise ValueError("probabilities and labels must have equal non-zero length")
    if any(not 0.0 <= probability <= 1.0 for probability in probabilities):
        raise ValueError("probabilities must be within [0, 1]")
    if any(label not in (0, 1) for label in labels):
        raise ValueError("labels must be binary")
