from pathlib import Path

import pytest

from domains.air_us.model.evaluation import evaluate_csv, validate_temporal_rows

FIXTURE = Path(__file__).parent / "fixtures" / "model" / "bts_sample.csv"


def test_forward_evaluator_reports_baselines_metrics_and_subgroups() -> None:
    report = evaluate_csv(FIXTURE, windows=2)
    assert report["window_count"] == 2
    assert report["timestamp_basis"].startswith("flight_date")
    target = report["windows"][0]["targets"]["ArrDel15"]
    assert {
        "historical_overall",
        "historical_route",
        "historical_carrier",
        "regularized_logistic",
    } <= set(target)
    model = target["regularized_logistic"]
    assert 0 <= model["brier"] <= 1
    assert 0 <= model["prevalence"] <= 1
    assert len(model["reliability"]) == 10
    assert target["subgroups_by_carrier"]


def test_temporal_validator_rejects_future_weather_and_naive_prediction_timestamp() -> None:
    row = {
        "flight_date": "2025-01-01",
        "crs_dep_time": "08:00",
        "prediction_timestamp_utc": "2025-01-01T13:00:00Z",
        "weather_observation_utc": "2025-01-01T13:01:00Z",
    }
    with pytest.raises(ValueError, match="after prediction timestamp"):
        validate_temporal_rows([row])
    row["prediction_timestamp_utc"] = "2025-01-01T13:00:00"
    row["weather_observation_utc"] = ""
    with pytest.raises(ValueError, match="include a timezone"):
        validate_temporal_rows([row])


def test_temporal_validator_accepts_explicit_prior_weather() -> None:
    row = {
        "flight_date": "2025-01-01",
        "crs_dep_time": "08:00",
        "prediction_timestamp_utc": "2025-01-01T13:00:00Z",
        "weather_observation_utc": "2025-01-01T12:00:00Z",
    }
    assert validate_temporal_rows([row]) == "prediction_timestamp_utc"
