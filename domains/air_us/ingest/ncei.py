"""Parser for locally supplied NOAA NCEI LCD historical CSV data."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class NCEIIngestError(ValueError):
    """Raised when LCD observations cannot be interpreted safely."""


_MISSING = {"", "M", "-", "*", "////", "999.9", "9999.9", "99999", "999999"}
_WX_TOKEN = re.compile(r"\b[A-Z]{2,6}\b")


def _field(row: Mapping[str, object], *names: str) -> str:
    normalized = {str(key).strip().upper(): value for key, value in row.items()}
    for name in names:
        value = normalized.get(name.upper())
        if value is not None:
            return str(value).strip()
    return ""


def _number(value: str) -> float | None:
    raw = value.strip()
    if raw.upper() in _MISSING:
        return None
    # LCD values can include quality suffixes, e.g. "10.0V".
    match = re.match(r"^(-?\d+(?:\.\d+)?)", raw)
    if not match:
        return None
    return float(match.group(1))


def parse_ncei_rows(
    rows: Iterable[Mapping[str, object]], *, timezone_name: str | None = None
) -> tuple[dict[str, object], ...]:
    """Return normalized observations with UTC timestamps and contract units.

    LCD visibility is reported in miles, ceiling in feet, and wind in knots. Wind
    fields whose header identifies MPH are converted to knots.
    """

    try:
        station_timezone = ZoneInfo(timezone_name) if timezone_name else None
    except ZoneInfoNotFoundError as exc:
        raise NCEIIngestError(f"unknown IANA station time zone {timezone_name!r}") from exc
    observations: list[dict[str, object]] = []
    for line_no, row in enumerate(rows, start=2):
        raw_ts = _field(row, "DATE", "DATE_TIME")
        if not raw_ts:
            raise NCEIIngestError(f"missing DATE on row {line_no}")
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NCEIIngestError(f"invalid LCD DATE on row {line_no}: {raw_ts!r}") from exc
        if ts.tzinfo is None:
            if station_timezone is None:
                raise NCEIIngestError(
                    "LCD timestamps without an offset require the station IANA time zone"
                )
            ts = ts.replace(tzinfo=station_timezone)
        ts = ts.astimezone(UTC)
        ceiling = _number(_field(row, "HourlyCeilingHeight", "HOURLYCEILINGHEIGHT"))
        visibility = _number(_field(row, "HourlyVisibility", "HOURLYVISIBILITY"))
        wind_raw = _field(row, "HourlyWindSpeed", "HOURLYWINDSPEED")
        wind = _number(wind_raw)
        # LCD's HourlyWindSpeed is normally mph; some normalized snapshots use
        # HourlyWindSpeedKts explicitly.
        if wind is not None and _field(row, "HourlyWindSpeedKts"):
            wind = _number(_field(row, "HourlyWindSpeedKts"))
        elif wind is not None:
            wind *= 0.868976
        wx_raw = _field(row, "HourlyPresentWeatherType", "HOURLYPRESENTWEATHERTYPE")
        wx_codes = (
            tuple(sorted(set(_WX_TOKEN.findall(wx_raw.upper()))))
            if wx_raw.upper() not in _MISSING
            else ()
        )
        observations.append(
            {
                "ts": ts,
                "ceiling_ft": int(ceiling) if ceiling is not None and ceiling >= 0 else None,
                "visibility_mi": visibility if visibility is not None and visibility >= 0 else None,
                "wind_kt": wind if wind is not None and wind >= 0 else None,
                "wx_codes": wx_codes,
            }
        )
    return tuple(observations)


def load_ncei_csv(
    source: str | TextIO, *, timezone_name: str | None = None
) -> tuple[dict[str, object], ...]:
    """Parse an already acquired LCD CSV string/file object; never fetches data."""

    stream: TextIO = io.StringIO(source, newline="") if isinstance(source, str) else source
    return parse_ncei_rows(csv.DictReader(stream), timezone_name=timezone_name)
