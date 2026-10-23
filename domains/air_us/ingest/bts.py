"""Small, deterministic readers for historical BTS On-Time Performance rows.

These helpers consume a locally supplied CSV snapshot. They never download data.
The BTS schedule fields are airport-local clock times, so callers must provide IANA
time zones before rows can be converted to comparable UTC instants.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class BTSIngestError(ValueError):
    """Raised when a BTS snapshot cannot safely be interpreted."""


@dataclass(frozen=True)
class BTSFlight:
    """One historical BTS schedule row normalized to UTC."""

    carrier: str
    flight_no: str
    origin: str
    dest: str
    flight_date: date
    crs_dep: datetime
    crs_arr: datetime
    ontime: bool
    cancelled: bool
    arrival_delay_min: float | None = None


def _value(row: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _bts_time(value: str, flight_date: date) -> datetime:
    digits = value.strip().split(".", maxsplit=1)[0].zfill(4)
    if not digits.isdigit() or len(digits) != 4:
        raise BTSIngestError(f"invalid BTS HHMM time: {value!r}")
    hour, minute = int(digits[:2]), int(digits[2:])
    if hour == 24 and minute == 0:
        return datetime.combine(flight_date + timedelta(days=1), time.min)
    if hour > 23 or minute > 59:
        raise BTSIngestError(f"invalid BTS HHMM time: {value!r}")
    return datetime.combine(flight_date, time(hour, minute))


def _utc(local_dt: datetime, airport: str, airport_timezones: Mapping[str, str]) -> datetime:
    zone_name = airport_timezones.get(airport)
    if not zone_name:
        raise BTSIngestError(f"missing IANA time zone for airport {airport}")
    try:
        return local_dt.replace(tzinfo=ZoneInfo(zone_name)).astimezone(UTC)
    except ZoneInfoNotFoundError as exc:
        raise BTSIngestError(f"unknown IANA time zone {zone_name!r} for {airport}") from exc


def parse_bts_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    flight_date: date,
    airport_timezones: Mapping[str, str],
) -> tuple[BTSFlight, ...]:
    """Normalize selected date rows from a BTS CSV snapshot.

    Only scheduled flights are retained as alternatives. Cancelled records remain
    available as schedule evidence but are never emitted as candidate legs.
    """

    flights: list[BTSFlight] = []
    for line_no, row in enumerate(rows, start=2):
        raw_date = _value(row, "FlightDate", "FL_DATE")
        if raw_date:
            try:
                row_date = date.fromisoformat(raw_date[:10])
            except ValueError as exc:
                raise BTSIngestError(f"invalid FlightDate on row {line_no}") from exc
            if row_date != flight_date:
                continue
        carrier = _value(row, "Reporting_Airline", "UniqueCarrier", "OP_UNIQUE_CARRIER")
        flight_no = _value(row, "Flight_Number_Reporting_Airline", "FlightNum", "OP_CARRIER_FL_NUM")
        origin = _value(row, "Origin", "ORIGIN").upper()
        dest = _value(row, "Dest", "DEST").upper()
        dep_raw = _value(row, "CRSDepTime", "CRS_DEP_TIME")
        arr_raw = _value(row, "CRSArrTime", "CRS_ARR_TIME")
        if not all((carrier, flight_no, origin, dest, dep_raw, arr_raw)):
            # BTS exports may contain cancelled rows with no scheduled values. A
            # malformed usable row must not silently become a plausible schedule.
            raise BTSIngestError(f"required schedule field missing on row {line_no}")
        dep = _utc(_bts_time(dep_raw, flight_date), origin, airport_timezones)
        arr_local = _bts_time(arr_raw, flight_date)
        arr = _utc(arr_local, dest, airport_timezones)
        # Compare UTC instants because BTS clocks are local to different airports.
        # A westbound same-day arrival may have an earlier local clock value.
        while arr <= dep:
            arr_local += timedelta(days=1)
            arr = _utc(arr_local, dest, airport_timezones)
        cancelled = _value(row, "Cancelled", "CANCELLED") in {"1", "1.0", "True", "true"}
        arr_del15 = _value(row, "ArrDel15", "ARR_DEL15")
        raw_arrival_delay = _value(row, "ArrDelay", "ARR_DELAY")
        try:
            arrival_delay_min = float(raw_arrival_delay) if raw_arrival_delay else None
        except ValueError as exc:
            raise BTSIngestError(f"invalid ArrDelay on row {line_no}") from exc
        # BTS uses an empty ArrDel15 for cancelled flights. It is not treated as
        # on-time; candidate quality only uses non-cancelled observed rows.
        ontime = not cancelled and arr_del15 in {"0", "0.0"}
        flights.append(
            BTSFlight(
                carrier,
                flight_no,
                origin,
                dest,
                flight_date,
                dep,
                arr,
                ontime,
                cancelled,
                arrival_delay_min,
            )
        )
    return tuple(flights)


def load_bts_csv(
    source: str | TextIO,
    *,
    flight_date: date,
    airport_timezones: Mapping[str, str],
) -> tuple[BTSFlight, ...]:
    """Read a local BTS CSV text/file object; no network access is performed."""

    stream: TextIO = io.StringIO(source, newline="") if isinstance(source, str) else source
    return parse_bts_rows(
        csv.DictReader(stream), flight_date=flight_date, airport_timezones=airport_timezones
    )
