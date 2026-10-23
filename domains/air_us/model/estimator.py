"""Small pure-Python gradient-boosted decision-stump classifier."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(value, 35.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -35.0))
    return z / (1.0 + z)


@dataclass(frozen=True)
class Stump:
    feature_index: int
    threshold: float
    left_value: float
    right_value: float


class GradientBoostedBinaryClassifier:
    """Logistic gradient boosting with one-level trees and Newton leaf values."""

    def __init__(self, rounds: int = 12, learning_rate: float = 0.12) -> None:
        if rounds < 1 or not 0.0 < learning_rate <= 1.0:
            raise ValueError("rounds must be positive and learning_rate in (0, 1]")
        self.rounds = rounds
        self.learning_rate = learning_rate
        self.base_logit = 0.0
        self.stumps: list[Stump] = []

    def fit(self, matrix: Sequence[Sequence[float]], labels: Sequence[int]) -> None:
        if not matrix or len(matrix) != len(labels):
            raise ValueError("matrix and labels must be non-empty and have equal length")
        if len({len(row) for row in matrix}) != 1 or not matrix[0]:
            raise ValueError("feature matrix must be rectangular and non-empty")
        if set(labels) != {0, 1}:
            raise ValueError("each binary training target must contain both classes")
        positives = sum(labels)
        rate = (positives + 0.5) / (len(labels) + 1.0)
        self.base_logit = math.log(rate / (1.0 - rate))
        logits = [self.base_logit] * len(labels)
        self.stumps = []

        for _ in range(self.rounds):
            probabilities = [_sigmoid(value) for value in logits]
            residuals = [
                label - probability
                for label, probability in zip(labels, probabilities, strict=True)
            ]
            hessians = [
                max(probability * (1.0 - probability), 1e-6) for probability in probabilities
            ]
            best: tuple[float, int, float, float, float] | None = None
            width = len(matrix[0])
            for feature_index in range(width):
                ordered = sorted({row[feature_index] for row in matrix})
                if len(ordered) < 2:
                    continue
                thresholds = [
                    (left + right) / 2.0 for left, right in zip(ordered, ordered[1:], strict=False)
                ]
                if len(thresholds) > 16:
                    stride = (len(thresholds) - 1) / 15
                    thresholds = [thresholds[round(index * stride)] for index in range(16)]
                for threshold in thresholds:
                    left = [i for i, row in enumerate(matrix) if row[feature_index] <= threshold]
                    if not left or len(left) == len(labels):
                        continue
                    left_indices = set(left)
                    right = [i for i in range(len(labels)) if i not in left_indices]
                    left_grad = sum(residuals[i] for i in left)
                    right_grad = sum(residuals[i] for i in right)
                    left_hess = sum(hessians[i] for i in left) + 1.0
                    right_hess = sum(hessians[i] for i in right) + 1.0
                    left_value = left_grad / left_hess
                    right_value = right_grad / right_hess
                    gain = (left_grad * left_grad / left_hess) + (
                        right_grad * right_grad / right_hess
                    )
                    candidate = (gain, feature_index, threshold, left_value, right_value)
                    if best is None or candidate > best:
                        best = candidate
            if best is None or best[0] <= 1e-12:
                break
            _, feature_index, threshold, left_value, right_value = best
            stump = Stump(feature_index, threshold, left_value, right_value)
            self.stumps.append(stump)
            for row_index, row in enumerate(matrix):
                leaf_value = left_value if row[feature_index] <= threshold else right_value
                logits[row_index] += self.learning_rate * leaf_value

    def predict_proba(self, matrix: Sequence[Sequence[float]]) -> list[float]:
        if not self.stumps:
            raise RuntimeError("estimator has not been fitted")
        probabilities: list[float] = []
        for row in matrix:
            logit = self.base_logit
            for stump in self.stumps:
                leaf = (
                    stump.left_value
                    if row[stump.feature_index] <= stump.threshold
                    else stump.right_value
                )
                logit += self.learning_rate * leaf
            probabilities.append(_sigmoid(logit))
        return probabilities

    def contributions(self, row: Sequence[float], feature_names: Sequence[str]) -> dict[str, float]:
        result = {name: 0.0 for name in feature_names}
        for stump in self.stumps:
            leaf = (
                stump.left_value
                if row[stump.feature_index] <= stump.threshold
                else stump.right_value
            )
            result[feature_names[stump.feature_index]] += self.learning_rate * leaf
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "learning_rate": self.learning_rate,
            "base_logit": self.base_logit,
            "stumps": [
                {
                    "feature_index": stump.feature_index,
                    "threshold": stump.threshold,
                    "left_value": stump.left_value,
                    "right_value": stump.right_value,
                }
                for stump in self.stumps
            ],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> GradientBoostedBinaryClassifier:
        model = cls(rounds=int(value["rounds"]), learning_rate=float(value["learning_rate"]))
        model.base_logit = float(value["base_logit"])
        model.stumps = [Stump(**stump) for stump in value["stumps"]]
        return model
