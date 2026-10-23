"""Build the locked weather response from live NWS or historical NCEI data."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from core.contracts.tools import (
    WeatherContextRequest,
    WeatherContextResponse,
    WeatherMode,
    WeatherObservation,
)
from domains.air_us.ingest.ncei import parse_ncei_rows
from domains.air_us.ingest.nws import NWSClient, NWSIngestError, utc_now


class WeatherContextError(ValueError):
    """Raised when requested weather cannot be sourced or safely normalized."""


HistoricalProvider = Callable[[str, datetime, datetime], Iterable[Mapping[str, object]]]


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WeatherContextError(f"{name} must include a timezone")
    return value.astimezone(UTC)


def _number(value: object, *, unit: str | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, dict) and isinstance(value.get("value"), (int, float)):
        numeric = float(value["value"])
        unit = str(value.get("unitCode", unit or ""))
    else:
        return None
    if unit and unit.endswith("[m]"):
        return numeric * 3.28084
    if unit and unit.endswith("[mi_i]"):
        return numeric
    if unit and unit.endswith("[m/s]"):
        return numeric * 1.943844
    if unit and unit.endswith("[mi_i]/h"):
        return numeric * 0.868976
    return numeric


def _nws_observation(period: Mapping[str, Any]) -> dict[str, object] | None:
    start = period.get("startTime")
    if not isinstance(start, str):
        return None
    try:
        ts = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return None
    cloud_layers = period.get("cloudLayers") or []
    ceiling_values: list[float] = []
    if isinstance(cloud_layers, list):
        for layer in cloud_layers:
            if isinstance(layer, dict) and str(layer.get("amount", "")).upper() in {
                "BKN",
                "OVC",
                "VV",
            }:
                base = layer.get("base")
                value = _number(base)
                if value is not None and value >= 0:
                    # NWS cloud layer base uses meters above ground.
                    ceiling_values.append(value * 3.28084)
    visibility_raw = period.get("visibility")
    visibility = _number(visibility_raw)
    if (
        isinstance(visibility_raw, dict)
        and str(visibility_raw.get("unitCode", "")).endswith(("[m]", ":m"))
        and visibility is not None
    ):
        visibility /= 1609.344
    wind = _number(period.get("windSpeed"))
    if isinstance(period.get("windSpeed"), str):
        text = str(period["windSpeed"]).lower().replace("mph", "").strip()
        try:
            wind = float(text) * 0.868976
        except ValueError:
            wind = None
    forecast = str(period.get("shortForecast", ""))
    wx_codes: list[str] = []
    normalized = forecast.lower()
    for word, code in (
        ("thunder", "TS"),
        ("snow", "SN"),
        ("rain", "RA"),
        ("fog", "FG"),
        ("freezing", "FZ"),
    ):
        if word in normalized:
            wx_codes.append(code)
    return {
        "ts": ts.astimezone(UTC),
        "ceiling_ft": int(min(ceiling_values)) if ceiling_values else None,
        "visibility_mi": visibility,
        "wind_kt": wind,
        "wx_codes": tuple(wx_codes),
    }


def _adverse_flags(observations: tuple[WeatherObservation, ...]) -> tuple[str, ...]:
    flags: set[str] = set()
    if any(item.ceiling_ft is not None and item.ceiling_ft < 1000 for item in observations):
        flags.add("low_ceiling")
    if any(item.visibility_mi is not None and item.visibility_mi < 3 for item in observations):
        flags.add("low_visibility")
    if any(item.wind_kt is not None and item.wind_kt >= 25 for item in observations):
        flags.add("high_wind")
    if any(any(code.upper().startswith("TS") for code in item.wx_codes) for item in observations):
        flags.add("thunderstorm")
    if any(any(code.upper().startswith("SN") for code in item.wx_codes) for item in observations):
        flags.add("snow")
    return tuple(sorted(flags))


def weather_context(
    request: WeatherContextRequest,
    *,
    nws_client: NWSClient | None = None,
    historical_provider: HistoricalProvider | None = None,
    airport_stations: Mapping[str, str] | None = None,
    airport_timezones: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = utc_now,
) -> WeatherContextResponse:
    """Return forecast or historical observations according to the explicit mode.

    Dependencies are injected for tests and offline replay; this function itself
    performs network I/O only through an explicitly supplied NWS client.
    """

    start, end = _utc(request.window_start, "window_start"), _utc(request.window_end, "window_end")
    if end <= start:
        raise WeatherContextError("window_end must be after window_start")
    if request.mode is WeatherMode.LIVE:
        if nws_client is None:
            raise WeatherContextError("live mode requires an injected NWS client")
        as_of = _utc(now(), "current time")
        if end <= as_of:
            raise WeatherContextError("NWS live mode only supports current or future windows")
        try:
            periods = nws_client.hourly_forecast(request.airport)
        except NWSIngestError as exc:
            raise WeatherContextError(str(exc)) from exc
        normalized = [item for period in periods if (item := _nws_observation(period)) is not None]
        confidence = "forecast"
        source = "NWS"
    else:
        station = (airport_stations or {}).get(request.airport)
        if not station:
            raise WeatherContextError(f"no configured NCEI station for airport {request.airport}")
        if historical_provider is None:
            raise WeatherContextError("historical mode requires an injected NCEI data provider")
        station_timezone = (airport_timezones or {}).get(request.airport)
        if not station_timezone:
            raise WeatherContextError(
                f"no configured NCEI station time zone for airport {request.airport}"
            )
        rows = tuple(historical_provider(station, start, end))
        normalized = list(parse_ncei_rows(rows, timezone_name=station_timezone))
        as_of = _utc(now(), "current time")
        confidence = "observed"
        source = "NCEI-LCD"

    observations_list: list[WeatherObservation] = []
    for item in normalized:
        ts = item.get("ts")
        if isinstance(ts, datetime) and start <= ts < end:
            observations_list.append(WeatherObservation.model_validate(item))
    observations = tuple(observations_list)
    return WeatherContextResponse(
        source=source,
        observations=observations,
        adverse_flags=_adverse_flags(observations),
        confidence=confidence,
        as_of=as_of,
    )
