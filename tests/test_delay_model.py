from datetime import date, time
from pathlib import Path

import pytest

from core.contracts import DelayRiskRequest
from domains.air_us.model import train_delay_risk_model
from domains.air_us.model.pipeline import _split
from domains.air_us.tools.delay_risk import delay_risk

FIXTURE = Path(__file__).parent / "fixtures" / "model" / "bts_sample.csv"
SYNTHETIC_PROVENANCE = "synthetic BTS-like fixture; not empirical BTS observations"


def test_training_uses_chronological_date_split_and_two_targets() -> None:
    model = train_delay_risk_model(FIXTURE, training_data_provenance=SYNTHETIC_PROVENANCE)
    assert model.metadata["training_rows"] == 14
    assert model.metadata["validation_rows"] == 5
    assert model.metadata["test_rows"] == 5
    assert model.metadata["training_end"] < model.metadata["validation_start"]
    assert model.metadata["validation_end"] < model.metadata["test_start"]
    assert set(model.metrics) == {"delay", "cancel"}
    assert model.metadata["training_data_provenance"] == SYNTHETIC_PROVENANCE
    assert model.metadata["evaluation_metrics_source"] == "chronological_test_holdout"
    for target in model.metrics.values():
        assert target["validation"]["role"] == "calibration_and_conformal_diagnostic"
        assert target["validation"]["calibration_rows"] == 3
        assert target["validation"]["conformal_rows"] == 2
        assert target["validation"]["brier_calibrated"] >= 0
        assert len(target["test"]["reliability_calibrated"]) == 10


def test_chronological_split_rejects_out_of_order_rows() -> None:
    import csv

    with FIXTURE.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    rows[0], rows[1] = rows[1], rows[0]
    with pytest.raises(ValueError, match="chronological order"):
        _split(rows)


def test_chronological_split_uses_utc_days_across_local_date_boundaries() -> None:
    rows = [
        {
            "flight_date": local_day,
            "prediction_timestamp_utc": f"2025-01-{utc_day:02}T12:00:00+00:00",
        }
        for utc_day, local_day in (
            (1, "2025-01-01"),
            (2, "2025-01-02"),
            (3, "2025-01-01"),
            (4, "2025-01-03"),
            (5, "2025-01-02"),
        )
    ]

    train, validation, test = _split(rows)

    assert train == [0, 1, 2]
    assert validation == [3]
    assert test == [4]


def test_model_serialization_preserves_locked_prediction(tmp_path: Path) -> None:
    model = train_delay_risk_model(FIXTURE, training_data_provenance=SYNTHETIC_PROVENANCE)
    request = DelayRiskRequest(
        carrier="UA",
        origin="SFO",
        dest="ORD",
        flight_date=date(2026, 7, 15),
        crs_dep_time=time(17, 30),
        mode="historical",
    )
    expected = delay_risk(request, model)
    artifact = tmp_path / "delayrisk.json"
    model.save(artifact)
    restored = type(model).load(artifact)
    actual = delay_risk(request, restored)
    assert actual == expected
    assert actual.model_version == model.metadata["model_version"]
    assert actual.feature_pipeline_version == model.metadata["feature_pipeline_version"]
    assert (
        0 <= actual.conformal_interval[0] <= actual.p_delay_15 <= actual.conformal_interval[1] <= 1
    )
    low_bin, high_bin = (float(value) for value in actual.calibration_bin.split("-"))
    assert low_bin <= actual.p_delay_15 <= high_bin
    assert actual.top_features
    assert "baseline history" in actual.plain_english
