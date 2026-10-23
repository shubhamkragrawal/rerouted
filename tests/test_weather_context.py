from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.contracts.tools import WeatherContextRequest, WeatherMode
from domains.air_us.ingest.ncei import NCEIIngestError, load_ncei_csv
from domains.air_us.ingest.nws import NWSClient
from domains.air_us.tools.weather_context import WeatherContextError, weather_context

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeHTTP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.hourly = json.loads((FIXTURES / "nws" / "hourly.json").read_text())

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if "/points/" in url:
            return FakeResponse(
                {"properties": {"forecastHourly": "https://api.weather.gov/grid/hourly"}}
            )
        return FakeResponse(self.hourly)


def test_historical_ncei_weather_is_filtered_and_flagged() -> None:
    source = (FIXTURES / "ncei" / "sample.csv").read_text()
    parsed = load_ncei_csv(source, timezone_name="UTC")
    rows = tuple(csv.DictReader(io.StringIO(source)))
    assert len(parsed) == 2
    response = weather_context(
        WeatherContextRequest(
            airport="JFK",
            window_start=datetime(2026, 3, 14, 12, tzinfo=UTC),
            window_end=datetime(2026, 3, 14, 13, tzinfo=UTC),
            mode=WeatherMode.HISTORICAL,
        ),
        airport_stations={"JFK": "USW00094789"},
        airport_timezones={"JFK": "UTC"},
        historical_provider=lambda station, start, end: rows,
        now=lambda: datetime(2026, 10, 8, tzinfo=UTC),
    )
    assert response.source == "NCEI-LCD"
    assert response.confidence == "observed"
    assert len(response.observations) == 1
    assert response.observations[0].ceiling_ft == 500
    assert response.observations[0].wind_kt == pytest.approx(26.07, abs=0.02)
    assert response.adverse_flags == ("high_wind", "low_ceiling", "low_visibility", "thunderstorm")


def test_live_nws_uses_injected_http_and_required_user_agent() -> None:
    http = FakeHTTP()
    client = NWSClient(
        {"JFK": (40.64, -73.78)},
        user_agent="Rerouted/0.1 (contact: maintainer@example.test)",
        client=http,
    )
    response = weather_context(
        WeatherContextRequest(
            airport="JFK",
            window_start=datetime(2026, 10, 8, 16, tzinfo=UTC),
            window_end=datetime(2026, 10, 8, 18, tzinfo=UTC),
            mode=WeatherMode.LIVE,
        ),
        nws_client=client,
        now=lambda: datetime(2026, 10, 8, 15, tzinfo=UTC),
    )
    assert response.source == "NWS"
    assert response.confidence == "forecast"
    assert len(response.observations) == 2
    assert response.observations[0].ceiling_ft == 492
    assert response.observations[0].visibility_mi == pytest.approx(0.621, abs=0.002)
    assert response.adverse_flags == ("high_wind", "low_ceiling", "low_visibility", "thunderstorm")
    assert len(http.calls) == 2
    assert all(call[1]["headers"]["User-Agent"].startswith("Rerouted/") for call in http.calls)


def test_live_requires_client_and_nws_user_agent_requires_contact() -> None:
    request = WeatherContextRequest(
        airport="JFK",
        window_start=datetime(2026, 10, 8, 16, tzinfo=UTC),
        window_end=datetime(2026, 10, 8, 18, tzinfo=UTC),
        mode=WeatherMode.LIVE,
    )
    with pytest.raises(WeatherContextError, match="requires an injected NWS client"):
        weather_context(request)
    with pytest.raises(ValueError, match="contact"):
        NWSClient({"JFK": (40.64, -73.78)}, user_agent="Rerouted")


def test_weather_rejects_naive_timestamps() -> None:
    request = WeatherContextRequest(
        airport="JFK",
        window_start=datetime(2026, 10, 8, 16),
        window_end=datetime(2026, 10, 8, 18),
        mode=WeatherMode.HISTORICAL,
    )
    with pytest.raises(WeatherContextError, match="must include a timezone"):
        weather_context(request)


def test_ncei_naive_timestamp_requires_station_timezone() -> None:
    with pytest.raises(NCEIIngestError, match="require the station IANA time zone"):
        load_ncei_csv("DATE,HourlyVisibility\n2026-03-14T12:00:00,10.0\n")
