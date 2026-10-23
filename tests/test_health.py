from __future__ import annotations

import shutil
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from fastapi.testclient import TestClient

from api.main import create_app
from domains.air_us.bootstrap import AirUSServices

FIXTURES = Path(__file__).parent / "fixtures"


def _services_with_schedule_and_weather(tmp_path: Path) -> AirUSServices:
    (tmp_path / "data/bts").mkdir(parents=True)
    (tmp_path / "data/ncei").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "bts/sample.csv", tmp_path / "data/bts/2026-03.csv")
    shutil.copyfile(FIXTURES / "ncei/sample.csv", tmp_path / "data/ncei/USW00094789.csv")
    config = {
        "paths": {
            "bts_schedule_template": "data/bts/{year}-{month:02d}.csv",
            "ncei_station_template": "data/ncei/{station}.csv",
        },
        "airports": {
            "JFK": {"timezone": "America/New_York", "ncei_station": "USW00094789"}
        },
        "nws": {"user_agent": "Rerouted/0.1 (contact: test@example.test)"},
    }
    config_path = tmp_path / "config/default.yaml"
    config_path.parent.mkdir()
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return AirUSServices(config_path=config_path)


def _services_without_schedule_or_weather(tmp_path: Path) -> AirUSServices:
    config_path = tmp_path / "config/default.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        yaml.safe_dump({"paths": {}, "airports": {"JFK": {"timezone": "America/New_York"}}}),
        encoding="utf-8",
    )
    return AirUSServices(config_path=config_path)


def test_health_reports_fixture_backed_artifacts_as_ready(tmp_path: Path) -> None:
    services = _services_with_schedule_and_weather(tmp_path)
    payload = TestClient(create_app(services)).get("/health").json()

    assert payload["status"] == "degraded"
    assert payload["service"] == "rerouted-api"
    assert payload["version"] == "0.1.0"
    assert payload["components"]["mcp"] == {
        "ready": True,
        "detail": "official MCP Python SDK v2 server configured",
    }
    assert payload["components"]["weather_historical"] == {
        "ready": True,
        "detail": "NCEI station files and airport time zones configured",
    }
    assert payload["components"]["alternatives"] == {
        "ready": True,
        "detail": "local historical BTS schedule and airport time zones configured",
    }
    assert payload["components"]["weather_live"]["ready"] is False


def test_health_reports_missing_artifacts_as_not_ready(tmp_path: Path) -> None:
    services = _services_without_schedule_or_weather(tmp_path)
    payload = TestClient(create_app(services)).get("/health").json()

    assert payload["status"] == "degraded"
    assert payload["components"]["weather_historical"]["ready"] is False
    assert payload["components"]["alternatives"]["ready"] is False
    assert payload["components"]["weather_historical"]["detail"] == (
        "NCEI CSV data, station IDs, and airport time zones must be configured"
    )
    assert payload["components"]["alternatives"]["detail"] == (
        "local BTS CSV schedule data and airport time zones must be configured"
    )
