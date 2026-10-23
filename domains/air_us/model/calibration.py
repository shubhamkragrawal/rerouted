"""Validation-fold isotonic calibration and conformal-style residual intervals."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any


class IsotonicCalibrator:
    """Pool-adjacent-violators calibrator with a deterministic step function."""

    def __init__(self) -> None:
        self.thresholds: list[float] = []
        self.values: list[float] = []

    def fit(self, probabilities: Sequence[float], labels: Sequence[int]) -> None:
        if not probabilities or len(probabilities) != len(labels):
            raise ValueError("calibration probabilities and labels must have equal non-zero length")
        blocks: list[list[float]] = []
        for probability, label in sorted(zip(probabilities, labels, strict=True)):
            blocks.append([probability, probability, float(label), 1.0])
            while len(blocks) > 1 and blocks[-2][2] / blocks[-2][3] > blocks[-1][2] / blocks[-1][3]:
                right = blocks.pop()
                left = blocks.pop()
                blocks.append([left[0], right[1], left[2] + right[2], left[3] + right[3]])
        self.thresholds = [block[1] for block in blocks]
        self.values = [min(1.0, max(0.0, block[2] / block[3])) for block in blocks]

    def predict(self, probabilities: Sequence[float]) -> list[float]:
        if not self.thresholds:
            raise RuntimeError("calibrator has not been fitted")
        result: list[float] = []
        for probability in probabilities:
            index = next(
                (i for i, threshold in enumerate(self.thresholds) if probability <= threshold),
                len(self.thresholds) - 1,
            )
            result.append(self.values[index])
        return result

    def to_dict(self) -> dict[str, list[float]]:
        return {"thresholds": self.thresholds, "values": self.values}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> IsotonicCalibrator:
        result = cls()
        result.thresholds = [float(item) for item in value["thresholds"]]
        result.values = [float(item) for item in value["values"]]
        return result


def residual_quantile(
    probabilities: Sequence[float], labels: Sequence[int], alpha: float = 0.1
) -> float:
    """Finite-sample absolute-error quantile, used to form clipped risk intervals."""
    if not probabilities or len(probabilities) != len(labels) or not 0.0 < alpha < 1.0:
        raise ValueError("non-empty aligned samples and alpha in (0, 1) are required")
    errors = sorted(
        abs(float(label) - probability)
        for probability, label in zip(probabilities, labels, strict=True)
    )
    rank = min(len(errors), math.ceil((len(errors) + 1) * (1.0 - alpha)))
    return errors[rank - 1]


def prediction_interval(probability: float, radius: float) -> tuple[float, float]:
    return (max(0.0, probability - radius), min(1.0, probability + radius))
