"""Local, deterministic forward-window comparison for predictive-model studies.

This module is deliberately separate from production training and never writes a model.
It uses only the Python standard library so evaluation remains available offline.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .metrics import brier_score, expected_calibration_error, reliability_curve

NUMERIC_FEATURES = (
    "route_historical_ontime_rate",
    "carrier_origin_historical_rate",
    "aircraft_turn_delay_proxy",
    "adverse_weather",
)


def _timestamp(row: dict[str, str]) -> datetime:
    """Read explicit UTC timestamp, or derive local scheduled time for older CSVs."""
    value = row.get("prediction_timestamp_utc", "").strip()
    if value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("prediction_timestamp_utc must include a timezone")
        return parsed
    # Legacy fixture/training CSVs omit UTC and airport timezone. This value is only
    # a stable ordering key; reports identify it as a local scheduled-time proxy.
    return datetime.fromisoformat(f"{row['flight_date']}T{row['crs_dep_time']}")


def validate_temporal_rows(rows: list[dict[str, str]]) -> str:
    """Validate ordering and any available source-time lineage against prediction time."""
    if not rows:
        raise ValueError("evaluation dataset is empty")
    stamps = [_timestamp(row) for row in rows]
    if stamps != sorted(stamps):
        raise ValueError("rows must be sorted by prediction timestamp")
    has_utc = all(row.get("prediction_timestamp_utc", "").strip() for row in rows)
    for row, prediction in zip(rows, stamps, strict=True):
        weather_stamp = row.get("weather_observation_utc", "").strip()
        if weather_stamp:
            weather = datetime.fromisoformat(weather_stamp.replace("Z", "+00:00"))
            if weather.tzinfo is None or prediction.tzinfo is None:
                raise ValueError("weather leakage check requires timezone-aware UTC timestamps")
            if weather > prediction:
                raise ValueError("weather observation occurs after prediction timestamp")
    return (
        "prediction_timestamp_utc"
        if has_utc
        else "flight_date + crs_dep_time (local ordering proxy)"
    )


def _auc(labels: list[int], scores: list[float]) -> float | None:
    positives, negatives = sum(labels), len(labels) - sum(labels)
    if not positives or not negatives:
        return None
    ordered = sorted(zip(scores, labels, strict=True))
    rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        average_rank = (index + 1 + end) / 2
        rank_sum += average_rank * sum(label for _, label in ordered[index:end])
        index = end
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def _average_precision(labels: list[int], scores: list[float]) -> float | None:
    positives = sum(labels)
    if not positives:
        return None
    ordered = sorted(zip(scores, labels, strict=True), reverse=True)
    seen = hits = 0
    area = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        group_labels = [label for _, label in ordered[index:end]]
        hits += sum(group_labels)
        seen += len(group_labels)
        area += (hits / seen) * (sum(group_labels) / positives)
        index = end
    return area / positives


def _metrics(labels: list[int], scores: list[float]) -> dict[str, Any]:
    threshold = 0.5
    tp = sum(y == 1 and p >= threshold for y, p in zip(labels, scores, strict=True))
    fp = sum(y == 0 and p >= threshold for y, p in zip(labels, scores, strict=True))
    fn = sum(y == 1 and p < threshold for y, p in zip(labels, scores, strict=True))
    return {
        "rows": len(labels),
        "prevalence": sum(labels) / len(labels),
        "auroc": _auc(labels, scores),
        "average_precision": _average_precision(labels, scores),
        "brier": brier_score(scores, labels),
        "ece": expected_calibration_error(scores, labels),
        "reliability": reliability_curve(scores, labels),
        "threshold": threshold,
        "utility_tp1_fp-1_fn-1": tp - fp - fn,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
    }


def _logistic_fit(matrix: list[list[float]], labels: list[int], *, l2: float = 1.0) -> list[float]:
    """Fit small L2-regularized logistic regression with batch gradient descent."""
    width = len(matrix[0]) + 1
    weights = [0.0] * width
    for step in range(900):
        gradient = [0.0] * width
        for row, label in zip(matrix, labels, strict=True):
            z = weights[0] + sum(w * x for w, x in zip(weights[1:], row, strict=True))
            probability = 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))
            error = probability - label
            gradient[0] += error
            for j, value in enumerate(row, start=1):
                gradient[j] += error * value
        rate = 0.15 / math.sqrt(1 + step / 30)
        weights[0] -= rate * gradient[0] / len(labels)
        for j in range(1, width):
            weights[j] -= rate * (gradient[j] / len(labels) + l2 * weights[j] / len(labels))
    return weights


def _predict_logistic(weights: list[float], matrix: list[list[float]]) -> list[float]:
    probabilities = []
    for row in matrix:
        logit = weights[0] + sum(w * x for w, x in zip(weights[1:], row, strict=True))
        probabilities.append(1 / (1 + math.exp(-max(-30.0, min(30.0, logit)))))
    return probabilities


def _historical_rates(
    train: list[dict[str, str]], test: list[dict[str, str]], target: str, group: str | None
) -> list[float]:
    total = sum(int(row[target]) for row in train)
    global_rate = (total + 1) / (len(train) + 2)
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    if group:
        for row in train:
            key = f"{row['origin']}|{row['dest']}" if group == "route" else row[group]
            counts[key][0] += int(row[target])
            counts[key][1] += 1
    return [
        (counts[key][0] + 2 * global_rate) / (counts[key][1] + 2)
        if group and key in counts
        else global_rate
        for row in test
        for key in (
            [f"{row['origin']}|{row['dest']}" if group == "route" else row[group]]
            if group
            else [""]
        )
    ]


def evaluate_csv(path: str | Path, *, windows: int = 3) -> dict[str, Any]:
    """Compare overall/route/carrier rates and regularized logistic over forward windows."""
    with Path(path).open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    timestamp_basis = validate_temporal_rows(rows)
    days = sorted({row["flight_date"] for row in rows})
    if windows < 1 or len(days) < windows + 2:
        raise ValueError("need at least windows + 2 distinct dates")
    reports: list[dict[str, Any]] = []
    boundaries = [days[round((len(days) - 1) * (i + 1) / (windows + 1))] for i in range(windows)]
    for number, boundary in enumerate(boundaries, start=1):
        train = [row for row in rows if row["flight_date"] <= boundary]
        next_boundary = boundaries[number] if number < len(boundaries) else days[-1]
        test = [row for row in rows if boundary < row["flight_date"] <= next_boundary]
        if not train or not test:
            continue
        target_reports: dict[str, Any] = {}
        for target in ("ArrDel15", "Cancelled"):
            eligible_train = [r for r in train if r[target].strip() in {"0", "1"}]
            eligible_test = [r for r in test if r[target].strip() in {"0", "1"}]
            if (
                not eligible_train
                or not eligible_test
                or len({int(r[target]) for r in eligible_train}) < 2
            ):
                continue
            labels = [int(r[target]) for r in eligible_test]
            matrix_train = [
                [float(row[name]) for name in NUMERIC_FEATURES] for row in eligible_train
            ]
            matrix_test = [[float(row[name]) for name in NUMERIC_FEATURES] for row in eligible_test]
            methods = {
                "historical_overall": _historical_rates(
                    eligible_train, eligible_test, target, None
                ),
                "historical_route": _historical_rates(
                    eligible_train, eligible_test, target, "route"
                ),
                "historical_carrier": _historical_rates(
                    eligible_train, eligible_test, target, "carrier"
                ),
            }
            y_train = [int(r[target]) for r in eligible_train]
            weights = _logistic_fit(matrix_train, y_train)
            methods["regularized_logistic"] = _predict_logistic(weights, matrix_test)
            target_reports[target] = {
                name: _metrics(labels, scores) for name, scores in methods.items()
            }
            target_reports[target]["subgroups_by_carrier"] = {
                carrier: _metrics(
                    [int(r[target]) for r in eligible_test if r["carrier"] == carrier],
                    [
                        methods["regularized_logistic"][i]
                        for i, row in enumerate(eligible_test)
                        if row["carrier"] == carrier
                    ],
                )
                for carrier in sorted({r["carrier"] for r in eligible_test})
            }
        reports.append(
            {
                "window": number,
                "train_through": boundary,
                "test_through": next_boundary,
                "targets": target_reports,
            }
        )
    return {
        "data": str(path),
        "timestamp_basis": timestamp_basis,
        "temporal_validation": (
            "available weather timestamps must be <= prediction timestamp; "
            "historical feature strictness is inherited from the dataset builder"
        ),
        "window_count": len(reports),
        "windows": reports,
        "limitations": [
            "legacy CSVs lack explicit UTC prediction timestamps",
            "logistic baseline uses numeric predeparture features only",
            "tree-boosting comparisons are not run by this offline standard-library evaluator",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--windows", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(evaluate_csv(args.dataset, windows=args.windows), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
