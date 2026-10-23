"""Chronological training, calibration, evaluation, and serialization."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from core.contracts import DelayRiskRequest, DelayRiskResponse
from core.contracts.tools import FeatureContribution

from .calibration import IsotonicCalibrator, prediction_interval, residual_quantile
from .estimator import GradientBoostedBinaryClassifier
from .features import (
    FEATURE_NAMES,
    FEATURE_PIPELINE_VERSION,
    FlightFeatures,
    build_features,
    request_features,
)
from .metrics import brier_score, expected_calibration_error, reliability_curve

TARGETS = {"delay": "ArrDel15", "cancel": "Cancelled"}
MODEL_VERSION = "delayrisk-0.1.0"
CATEGORICAL_FEATURES = {"carrier", "origin", "dest"}
MAX_TRAIN_ROWS = 50_000
MAX_VALIDATION_ROWS = 20_000
MAX_TEST_ROWS = 50_000


@dataclass
class FeatureEncoder:
    categories: dict[str, list[str]]
    feature_names: list[str]

    @classmethod
    def fit(cls, rows: Sequence[FlightFeatures]) -> FeatureEncoder:
        categories = {
            name: sorted({str(row[name]) for row in rows})
            for name in FEATURE_NAMES
            if name in CATEGORICAL_FEATURES
        }
        names = [
            f"{feature}={category}" for feature, levels in categories.items() for category in levels
        ]
        names.extend(name for name in FEATURE_NAMES if name not in CATEGORICAL_FEATURES)
        return cls(categories=categories, feature_names=names)

    def transform(self, rows: Sequence[FlightFeatures]) -> list[list[float]]:
        matrix: list[list[float]] = []
        for row in rows:
            encoded = [
                float(str(row[feature]) == category)
                for feature, levels in self.categories.items()
                for category in levels
            ]
            encoded.extend(
                float(row[name]) for name in FEATURE_NAMES if name not in CATEGORICAL_FEATURES
            )
            matrix.append(encoded)
        return matrix

    def to_dict(self) -> dict[str, Any]:
        return {"categories": self.categories, "feature_names": self.feature_names}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FeatureEncoder:
        return cls(
            categories={key: list(levels) for key, levels in value["categories"].items()},
            feature_names=list(value["feature_names"]),
        )


@dataclass
class BinaryRiskModel:
    estimator: GradientBoostedBinaryClassifier
    calibrator: IsotonicCalibrator
    interval_radius: float
    use_calibration: bool = True

    def probabilities(self, matrix: Sequence[Sequence[float]]) -> tuple[list[float], list[float]]:
        raw = self.estimator.predict_proba(matrix)
        calibrated = self.calibrator.predict(raw) if self.use_calibration else list(raw)
        return raw, calibrated

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimator": self.estimator.to_dict(),
            "calibrator": self.calibrator.to_dict(),
            "interval_radius": self.interval_radius,
            "use_calibration": self.use_calibration,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BinaryRiskModel:
        return cls(
            estimator=GradientBoostedBinaryClassifier.from_dict(value["estimator"]),
            calibrator=IsotonicCalibrator.from_dict(value["calibrator"]),
            interval_radius=float(value["interval_radius"]),
            use_calibration=bool(value.get("use_calibration", True)),
        )


@dataclass
class DelayRiskModel:
    encoder: FeatureEncoder
    delay: BinaryRiskModel
    cancel: BinaryRiskModel
    metadata: dict[str, Any]
    metrics: dict[str, Any]

    def response(
        self, features: FlightFeatures, *, baseline_inputs: bool = False
    ) -> DelayRiskResponse:
        matrix = self.encoder.transform([features])
        raw_delay, calibrated_delay = self.delay.probabilities(matrix)
        _, calibrated_cancel = self.cancel.probabilities(matrix)
        del raw_delay
        probability = calibrated_delay[0]
        explanations: dict[str, float] = {}
        contributions = self.delay.estimator.contributions(matrix[0], self.encoder.feature_names)
        for encoded_name, value in contributions.items():
            feature_name = encoded_name.split("=", 1)[0]
            explanations[feature_name] = explanations.get(feature_name, 0.0) + value
        top_features = tuple(
            FeatureContribution(feature=name, contribution=round(value, 6))
            for name, value in sorted(
                explanations.items(), key=lambda item: (-abs(item[1]), item[0])
            )[:5]
        )
        low, high = prediction_interval(probability, self.delay.interval_radius)
        lower_bin = min(0.9, int(probability * 10) / 10)
        upper_bin = min(1.0, lower_bin + 0.1)
        calibration_bin = f"{lower_bin:.2f}-{upper_bin:.2f}"
        percentage = round(probability * 100)
        baseline_note = (
            "Using baseline history and no adverse-weather flag, " if baseline_inputs else ""
        )
        return DelayRiskResponse(
            p_delay_15=probability,
            p_cancel=calibrated_cancel[0],
            conformal_interval=(low, high),
            calibration_bin=calibration_bin,
            top_features=top_features,
            plain_english=(
                f"{baseline_note}the model estimates about a {percentage}% chance of "
                "arrival delay of 15 minutes or more."
            ),
            model_version=str(self.metadata["model_version"]),
            feature_pipeline_version=str(self.metadata["feature_pipeline_version"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata,
            "encoder": self.encoder.to_dict(),
            "models": {"delay": self.delay.to_dict(), "cancel": self.cancel.to_dict()},
            "metrics": self.metrics,
        }

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path) -> DelayRiskModel:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DelayRiskModel:
        return cls(
            encoder=FeatureEncoder.from_dict(value["encoder"]),
            delay=BinaryRiskModel.from_dict(value["models"]["delay"]),
            cancel=BinaryRiskModel.from_dict(value["models"]["cancel"]),
            metadata=dict(value["metadata"]),
            metrics=dict(value["metrics"]),
        )


def _read_rows(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _features(row: dict[str, str]) -> FlightFeatures:
    return build_features(
        carrier=row["carrier"],
        origin=row["origin"],
        dest=row["dest"],
        flight_date=date.fromisoformat(row["flight_date"]),
        crs_dep_time=time.fromisoformat(row["crs_dep_time"]),
        route_historical_ontime_rate=float(row["route_historical_ontime_rate"]),
        carrier_origin_historical_rate=float(row["carrier_origin_historical_rate"]),
        aircraft_turn_delay_proxy=float(row["aircraft_turn_delay_proxy"]),
        adverse_weather=row["adverse_weather"] == "1",
    )


def _split(rows: list[dict[str, str]]) -> tuple[list[int], list[int], list[int]]:
    timestamps = [row.get("prediction_timestamp_utc", "").strip() for row in rows]
    has_timestamps = any(timestamps)
    if has_timestamps and not all(timestamps):
        raise ValueError("prediction timestamps must be present on every training row")
    if has_timestamps:
        prediction_times = [
            datetime.fromisoformat(value.replace("Z", "+00:00")) for value in timestamps
        ]
        if any(stamp.tzinfo is None for stamp in prediction_times):
            raise ValueError("prediction timestamps must include a UTC offset")
        prediction_times = [stamp.astimezone(UTC) for stamp in prediction_times]
        if prediction_times != sorted(prediction_times):
            raise ValueError("training rows must be ordered by prediction timestamp")
        # Group whole UTC days to avoid splitting simultaneous/nearby flights
        # across folds while keeping the global UTC chronology authoritative.
        split_days = [stamp.date() for stamp in prediction_times]
    else:
        split_days = [date.fromisoformat(row["flight_date"]) for row in rows]
        if split_days != sorted(split_days):
            raise ValueError("legacy training rows must be in chronological order")
    unique_dates = sorted(set(split_days))
    if len(unique_dates) < 5:
        raise ValueError("chronological split requires at least five distinct days")
    train_boundary = unique_dates[max(1, int(len(unique_dates) * 0.6)) - 1]
    validation_boundary = unique_dates[max(2, int(len(unique_dates) * 0.8)) - 1]
    train = [i for i, day in enumerate(split_days) if day <= train_boundary]
    validation = [
        i for i, day in enumerate(split_days) if train_boundary < day <= validation_boundary
    ]
    test = [i for i, day in enumerate(split_days) if day > validation_boundary]
    if not train or not validation or not test:
        raise ValueError("chronological 60/20/20 day split produced an empty fold")
    if has_timestamps:
        if max(prediction_times[i] for i in train) >= min(
            prediction_times[i] for i in validation
        ):
            raise ValueError("training prediction timestamps must precede validation")
        if max(prediction_times[i] for i in validation) >= min(
            prediction_times[i] for i in test
        ):
            raise ValueError("validation prediction timestamps must precede test")
    else:
        if max(split_days[i] for i in train) >= min(split_days[i] for i in validation):
            raise ValueError("training dates must precede validation dates")
        if max(split_days[i] for i in validation) >= min(split_days[i] for i in test):
            raise ValueError("validation dates must precede test dates")
    return train, validation, test


def _even_sample(indices: list[int], limit: int) -> list[int]:
    """Return a deterministic chronological sample including both endpoints."""

    if len(indices) <= limit:
        return indices
    step = (len(indices) - 1) / (limit - 1)
    return [indices[round(position * step)] for position in range(limit)]


def _fit_target(
    matrix: list[list[float]], labels: list[int], train: list[int], validation: list[int]
) -> tuple[BinaryRiskModel, dict[str, Any]]:
    if len(validation) < 2:
        raise ValueError("validation fold needs separate calibration and conformal rows")
    calibration_indices = validation[::2]
    conformal_indices = validation[1::2]
    estimator = GradientBoostedBinaryClassifier()
    estimator.fit([matrix[i] for i in train], [labels[i] for i in train])
    raw_calibration = estimator.predict_proba([matrix[i] for i in calibration_indices])
    calibration_labels = [labels[i] for i in calibration_indices]
    calibrator = IsotonicCalibrator()
    calibrator.fit(raw_calibration, calibration_labels)
    raw_conformal = estimator.predict_proba([matrix[i] for i in conformal_indices])
    conformal_labels = [labels[i] for i in conformal_indices]
    calibrated_conformal = calibrator.predict(raw_conformal)
    use_calibration = brier_score(calibrated_conformal, conformal_labels) <= brier_score(
        raw_conformal, conformal_labels
    )
    effective_conformal = calibrated_conformal if use_calibration else raw_conformal
    radius = residual_quantile(effective_conformal, conformal_labels)
    raw_validation = estimator.predict_proba([matrix[i] for i in validation])
    calibrated_validation = (
        calibrator.predict(raw_validation) if use_calibration else list(raw_validation)
    )
    validation_labels = [labels[i] for i in validation]
    report = {
        "validation": {
            "role": "calibration_and_conformal_diagnostic",
            "interpretation": (
                "The fold is divided into disjoint isotonic-calibration and split-"
                "conformal subsets. Use the chronological test metrics for evaluation."
            ),
            "calibration_rows": len(calibration_indices),
            "conformal_rows": len(conformal_indices),
            "isotonic_selected": use_calibration,
            "brier_uncalibrated": brier_score(raw_validation, validation_labels),
            "brier_calibrated": brier_score(calibrated_validation, validation_labels),
            "ece_uncalibrated": expected_calibration_error(raw_validation, validation_labels),
            "ece_calibrated": expected_calibration_error(calibrated_validation, validation_labels),
            "reliability_uncalibrated": reliability_curve(raw_validation, validation_labels),
            "reliability_calibrated": reliability_curve(calibrated_validation, validation_labels),
        }
    }
    return BinaryRiskModel(estimator, calibrator, radius, use_calibration), report


def train_delay_risk_model(
    path: str | Path,
    *,
    training_data_provenance: str = "unspecified; source provenance has not been attested",
) -> DelayRiskModel:
    """Train deterministic delay and cancellation models with temporal holdouts.

    The caller must describe the dataset honestly; metrics do not attest source quality.
    """
    rows = _read_rows(path)
    source_rows = len(rows)
    source_train, source_validation, source_test = _split(rows)
    sampled_folds = (
        _even_sample(source_train, MAX_TRAIN_ROWS),
        _even_sample(source_validation, MAX_VALIDATION_ROWS),
        _even_sample(source_test, MAX_TEST_ROWS),
    )
    selected = [index for fold in sampled_folds for index in fold]
    rows = [rows[index] for index in selected]
    train_end = len(sampled_folds[0])
    validation_end = train_end + len(sampled_folds[1])
    train = list(range(train_end))
    validation = list(range(train_end, validation_end))
    test = list(range(validation_end, len(rows)))
    features = [_features(row) for row in rows]
    encoder = FeatureEncoder.fit([features[i] for i in train])
    matrix = encoder.transform(features)
    fitted: dict[str, BinaryRiskModel] = {}
    reports: dict[str, Any] = {}
    for model_name, target in TARGETS.items():
        raw_labels = [row[target].strip() for row in rows]
        if model_name == "delay":
            eligible = [i for i in range(len(rows)) if raw_labels[i] in {"0", "0.0", "1", "1.0"}]
            target_name = f"{target} on rows with observed arrival-delay labels"
        else:
            eligible = list(range(len(rows)))
            target_name = target
        if any(raw_labels[i] not in {"0", "0.0", "1", "1.0"} for i in eligible):
            raise ValueError(f"target {target} must be binary on eligible rows")
        labels = [
            int(float(value)) if value in {"0", "0.0", "1", "1.0"} else 0 for value in raw_labels
        ]
        eligible_set = set(eligible)
        target_train = [i for i in train if i in eligible_set]
        target_validation = [i for i in validation if i in eligible_set]
        target_test = [i for i in test if i in eligible_set]
        if not target_train or len(target_validation) < 2 or not target_test:
            raise ValueError(f"insufficient eligible rows for {target_name} in chronological folds")
        fitted[model_name], reports[model_name] = _fit_target(
            matrix, labels, target_train, target_validation
        )
        test_raw, test_calibrated = fitted[model_name].probabilities(
            [matrix[i] for i in target_test]
        )
        test_labels = [labels[i] for i in target_test]
        reports[model_name]["test"] = {
            "brier_uncalibrated": brier_score(test_raw, test_labels),
            "brier_calibrated": brier_score(test_calibrated, test_labels),
            "ece_uncalibrated": expected_calibration_error(test_raw, test_labels),
            "ece_calibrated": expected_calibration_error(test_calibrated, test_labels),
            "reliability_uncalibrated": reliability_curve(test_raw, test_labels),
            "reliability_calibrated": reliability_curve(test_calibrated, test_labels),
            "eligible_rows": len(target_test),
            "eligible_rows_total": len(eligible),
            "target_definition": target_name,
        }
        reports[model_name]["training_rows"] = len(target_train)
        reports[model_name]["validation_rows"] = len(target_validation)
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    metadata = {
        "model_version": MODEL_VERSION,
        "feature_pipeline_version": FEATURE_PIPELINE_VERSION,
        "estimator": "pure-python-logistic-gradient-boosted-stumps",
        "calibration": "isotonic selected only when conformal-fold Brier score improves",
        "uncertainty_interval": (
            "clipped split-conformal 90% absolute-residual interval; isotonic calibration "
            "and conformal residuals use disjoint chronological validation subsets"
        ),
        "training_rows": len(train),
        "validation_rows": len(validation),
        "test_rows": len(test),
        "training_start": rows[train[0]]["flight_date"],
        "training_end": rows[train[-1]]["flight_date"],
        "validation_start": rows[validation[0]]["flight_date"],
        "validation_end": rows[validation[-1]]["flight_date"],
        "test_start": rows[test[0]]["flight_date"],
        "test_end": rows[test[-1]]["flight_date"],
        "split_time_basis": (
            "prediction_timestamp_utc grouped by UTC date"
            if all(row.get("prediction_timestamp_utc", "").strip() for row in rows)
            else "flight_date"
        ),
        **(
            {
                f"{fold_name}_prediction_{edge}_utc": rows[index][
                    "prediction_timestamp_utc"
                ]
                for fold_name, indices in (
                    ("training", train),
                    ("validation", validation),
                    ("test", test),
                )
                for edge, index in (("start", indices[0]), ("end", indices[-1]))
            }
            if all(row.get("prediction_timestamp_utc", "").strip() for row in rows)
            else {}
        ),
        "source_dataset_rows": source_rows,
        "sampling": {
            "method": "deterministic_even_chronological",
            "maximum_train_rows": MAX_TRAIN_ROWS,
            "maximum_validation_rows": MAX_VALIDATION_ROWS,
            "maximum_test_rows": MAX_TEST_ROWS,
        },
        "training_data_sha256": hashlib.sha256(canonical).hexdigest(),
        "training_data_provenance": training_data_provenance,
        "evaluation_metrics_source": "chronological_test_holdout",
        "targets": TARGETS,
    }
    return DelayRiskModel(encoder, fitted["delay"], fitted["cancel"], metadata, reports)


def predict_request(
    model: DelayRiskModel,
    request: DelayRiskRequest,
    features: FlightFeatures | None = None,
) -> DelayRiskResponse:
    """Serve contract requests; accept leakage-safe historical/weather features if available."""
    if features is not None:
        return model.response(features)
    return model.response(request_features(request), baseline_inputs=True)
