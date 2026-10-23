"""Local-only BTS and NOAA LCD acquisition and analytical data pipeline.

Downloads remain ordinary files under ``data/raw``. No cloud services are used.
The pipeline requires explicit airport timezone and BTS-airport-to-LCD-station
reference CSVs so weather joins are auditable rather than proximity guesses.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import uuid
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from domains.air_us.ingest.ncei import load_ncei_csv
from domains.air_us.model.pipeline import train_delay_risk_model

BTS_REQUIRED = {
    "FlightDate",
    "Reporting_Airline",
    "Flight_Number_Reporting_Airline",
    "Origin",
    "Dest",
    "CRSDepTime",
    "CRSArrTime",
    "ArrDel15",
    "Cancelled",
}
# NOAA LCD exports are station-dependent. ``HourlyCeilingHeight`` is absent from
# some otherwise valid official station files (for example, stations that expose
# sky conditions but no derived ceiling). The parser already treats ceiling as an
# optional observation, so only fields required to timestamp and score the remaining
# weather signals belong in the hard schema gate.
LCD_REQUIRED = {"DATE", "HourlyVisibility", "HourlyWindSpeed"}
FEATURE_COLUMNS = (
    "carrier",
    "origin",
    "dest",
    "flight_date",
    "crs_dep_time",
    "route_historical_ontime_rate",
    "carrier_origin_historical_rate",
    "aircraft_turn_delay_proxy",
    "adverse_weather",
    "ArrDel15",
    "Cancelled",
)
DATASET_COLUMNS = (*FEATURE_COLUMNS, "prediction_timestamp_utc")
PIPELINE_VERSION = "local-real-data-4"


class DataPipelineError(ValueError):
    """Raised when source artifacts or pipeline inputs fail validation."""


@dataclass(frozen=True)
class SourceRecord:
    kind: str
    url: str
    path: str
    sha256: str
    size_bytes: int
    expected_sha256: str | None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _peak_rss_bytes() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _safe_download(url: str, destination: Path, expected_sha256: str | None = None) -> SourceRecord:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        digest = sha256_file(destination)
        if expected_sha256 and digest != expected_sha256.lower():
            raise DataPipelineError(f"SHA-256 mismatch for cached artifact {destination}")
        return SourceRecord(
            "", url, str(destination), digest, destination.stat().st_size, expected_sha256
        )
    fd, temporary_name = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "rerouted-local-data-pipeline/1.0"}
        )
        with (
            urllib.request.urlopen(request, timeout=90) as response,
            temporary.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
        digest = sha256_file(temporary)
        if expected_sha256 and digest != expected_sha256.lower():
            raise DataPipelineError(f"SHA-256 mismatch for downloaded artifact {url}")
        if temporary.stat().st_size == 0:
            raise DataPipelineError(f"download was empty: {url}")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return SourceRecord(
        "", url, str(destination), digest, destination.stat().st_size, expected_sha256
    )


def bts_month_url(year: int, month: int) -> str:
    if not 1987 <= year <= 2100 or not 1 <= month <= 12:
        raise ValueError("invalid BTS year/month")
    filename = f"On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip"
    return f"https://transtats.bts.gov/PREZIP/{filename}"


def lcd_year_url(year: int, station_id: str) -> str:
    if not 1900 <= year <= 2100 or not re.fullmatch(r"[A-Z0-9]{4,16}", station_id):
        raise ValueError("invalid LCD year or station identifier")
    return (
        f"https://www.ncei.noaa.gov/data/local-climatological-data/access/{year}/{station_id}.csv"
    )


def load_sha256_expectations(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not re.fullmatch(r"[0-9a-fA-F]{64}", value)
        for key, value in values.items()
    ):
        raise DataPipelineError("checksum file must map source URLs to 64-character SHA-256 values")
    return {key: value.lower() for key, value in values.items()}


def _csv_header(stream: Any) -> tuple[str, ...]:
    position = stream.tell()
    reader = csv.reader(stream)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise DataPipelineError("CSV source has no header") from exc
    finally:
        stream.seek(position)
    return tuple(column.strip().lstrip("\ufeff") for column in header)


def validate_bts_csv(path: Path) -> None:
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        missing = BTS_REQUIRED - set(_csv_header(stream))
        if missing:
            raise DataPipelineError(f"BTS schema is missing columns: {', '.join(sorted(missing))}")
        reader = csv.DictReader(stream)
        if not any(True for _ in reader):
            raise DataPipelineError(f"BTS CSV contains no data rows: {path}")


def validate_lcd_csv(path: Path) -> None:
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        missing = LCD_REQUIRED - set(_csv_header(stream))
        if missing:
            raise DataPipelineError(f"LCD schema is missing columns: {', '.join(sorted(missing))}")
        if not any(True for _ in csv.DictReader(stream)):
            raise DataPipelineError(f"LCD CSV contains no data rows: {path}")


def _csv_member(archive: zipfile.ZipFile, path: Path) -> str:
    try:
        bad_member = archive.testzip()
        if bad_member:
            raise DataPipelineError(f"BTS ZIP CRC failure in {bad_member}")
    except zipfile.BadZipFile as exc:
        raise DataPipelineError(f"invalid BTS ZIP file: {path}") from exc
    members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
    if len(members) != 1:
        raise DataPipelineError(f"expected one CSV in BTS ZIP, found {len(members)}")
    return members[0]


def validate_bts_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            member = _csv_member(archive, path)
            with (
                archive.open(member) as raw_stream,
                io.TextIOWrapper(raw_stream, encoding="utf-8-sig", newline="") as stream,
            ):
                reader = csv.DictReader(stream)
                missing = BTS_REQUIRED - set(reader.fieldnames or [])
                if missing:
                    raise DataPipelineError(
                        f"BTS schema is missing columns: {', '.join(sorted(missing))}"
                    )
                if not any(True for _ in reader):
                    raise DataPipelineError(f"BTS CSV contains no data rows: {path}")
    except zipfile.BadZipFile as exc:
        raise DataPipelineError(f"invalid BTS ZIP file: {path}") from exc


def fetch_sources(
    *,
    start: date,
    end: date,
    raw_dir: Path,
    mapping_path: Path,
    checksums_path: Path | None = None,
) -> list[SourceRecord]:
    if start > end:
        raise ValueError("start date must not follow end date")
    zones, stations = load_reference_tables(None, mapping_path)
    del zones
    expected = load_sha256_expectations(checksums_path)
    source_records: list[SourceRecord] = []
    acquisition_start = start - timedelta(days=365)
    month = date(acquisition_start.year, acquisition_start.month, 1)
    last = date(end.year, end.month, 1)
    while month <= last:
        url = bts_month_url(month.year, month.month)
        path = raw_dir / "bts" / f"{month.year}-{month.month:02}.zip"
        record = _safe_download(url, path, expected.get(url))
        record = SourceRecord(
            "bts_zip",
            record.url,
            record.path,
            record.sha256,
            record.size_bytes,
            record.expected_sha256,
        )
        validate_bts_zip(path)
        source_records.append(record)
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    for year in range(acquisition_start.year, end.year + 1):
        for station_id in sorted(set(stations.values())):
            url = lcd_year_url(year, station_id)
            path = raw_dir / "lcd" / str(year) / f"{station_id}.csv"
            record = _safe_download(url, path, expected.get(url))
            record = SourceRecord(
                "ncei_lcd_csv",
                record.url,
                record.path,
                record.sha256,
                record.size_bytes,
                record.expected_sha256,
            )
            validate_lcd_csv(path)
            source_records.append(record)
    return source_records


def load_reference_tables(
    timezone_path: Path | None, station_mapping_path: Path
) -> tuple[dict[str, str], dict[str, str]]:
    if timezone_path is None:
        timezone_path = Path(__file__).parents[2] / "config" / "airport_timezones.csv"
    zones: dict[str, str] = {}
    with timezone_path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not {"airport", "time_zone"}.issubset(reader.fieldnames or []):
            raise DataPipelineError("timezone reference requires airport,time_zone columns")
        for row in reader:
            airport, zone = row["airport"].strip().upper(), row["time_zone"].strip()
            if airport in zones:
                raise DataPipelineError(f"duplicate timezone mapping for {airport}")
            try:
                ZoneInfo(zone)
            except ZoneInfoNotFoundError as exc:
                raise DataPipelineError(f"invalid IANA timezone {zone!r} for {airport}") from exc
            zones[airport] = zone
    stations: dict[str, str] = {}
    with station_mapping_path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not {"airport", "station_id"}.issubset(reader.fieldnames or []):
            raise DataPipelineError("station mapping requires airport,station_id columns")
        for row in reader:
            airport, station = row["airport"].strip().upper(), row["station_id"].strip().upper()
            if airport in stations:
                raise DataPipelineError(f"duplicate weather station mapping for {airport}")
            if not airport or not re.fullmatch(r"[A-Z0-9]{4,16}", station):
                raise DataPipelineError(f"invalid station mapping row: {row}")
            stations[airport] = station
    if not zones or not stations:
        raise DataPipelineError(
            "airport timezone and weather station reference tables cannot be empty"
        )
    if set(stations) - set(zones):
        raise DataPipelineError("weather station mapping includes airports without timezones")
    return zones, stations


def load_carrier_reference(path: Path | None = None) -> dict[str, str]:
    if path is None:
        path = Path(__file__).parents[2] / "config" / "carrier_reference.csv"
    carriers: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not {"carrier_code", "carrier_name"}.issubset(reader.fieldnames or []):
            raise DataPipelineError("carrier reference requires carrier_code,carrier_name columns")
        for row in reader:
            code = row["carrier_code"].strip().upper()
            name = row["carrier_name"].strip()
            if not code or not name or code in carriers:
                raise DataPipelineError(f"invalid or duplicate carrier reference row: {row}")
            carriers[code] = name
    if not carriers:
        raise DataPipelineError("carrier reference cannot be empty")
    return carriers


def _scheduled_local(value: str, flight_day: date, *, field: str) -> datetime:
    """Parse a BTS HHMM value, including the documented ``2400`` boundary."""

    digits = value.strip().split(".", maxsplit=1)[0].zfill(4)
    if not digits.isdigit() or len(digits) != 4:
        raise DataPipelineError(f"invalid scheduled {field} time: {value!r}")
    hour, minute = int(digits[:2]), int(digits[2:])
    if hour == 24 and minute == 0:
        return datetime.combine(flight_day + timedelta(days=1), time.min)
    if hour > 23 or minute > 59:
        raise DataPipelineError(f"invalid scheduled {field} time: {value!r}")
    return datetime.combine(flight_day, time(hour, minute))


def _binary_bts_value(value: str, *, field: str, allow_blank: bool = False) -> int | None:
    """Normalize BTS binary fields across exports such as 0, 0.0, and 0.00."""

    stripped = value.strip()
    if not stripped and allow_blank:
        return None
    try:
        numeric = float(stripped)
    except ValueError as exc:
        raise DataPipelineError(f"invalid {field} value: {value!r}") from exc
    if numeric not in {0.0, 1.0}:
        raise DataPipelineError(f"invalid {field} value: {value!r}")
    return int(numeric)


def _bts_business_key(row: dict[str, str]) -> tuple[str, ...]:
    """Return the stable flight identity used to detect source duplicates."""
    fields = (
        "FlightDate",
        "Reporting_Airline",
        "Flight_Number_Reporting_Airline",
        "Origin",
        "Dest",
        "CRSDepTime",
    )
    return tuple(row.get(field, "").strip().upper() for field in fields)


def _bts_digest(values: Iterator[str] | tuple[str, ...]) -> bytes:
    """Hash a sequence unambiguously without retaining serialized row strings."""
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode("utf-8", errors="replace")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.digest()


def _bts_row_digests(row: dict[str, str]) -> tuple[bytes, bytes]:
    key_digest = _bts_digest(_bts_business_key(row))
    fingerprint_fields = tuple(
        f"{column}={row[column].strip()}" for column in sorted(row) if column is not None
    )
    return key_digest, _bts_digest(fingerprint_fields)


def _iter_bts_rows(
    raw_dir: Path,
    start: date,
    end: date,
    *,
    quality_counts: Counter[str] | None = None,
) -> Iterator[dict[str, str]]:
    """Yield deduplicated, date-filtered BTS rows without retaining source rows in RAM.

    Exact duplicate flight records can occur when monthly exports overlap. They are
    counted and emitted once. A duplicate flight identity with differing values is
    ambiguous and fails closed instead of silently selecting one version.
    """
    # BTS ZIPs are monthly exports. Keep compact key/fingerprint digests only
    # while source date ranges can overlap; discard a prior month's map once the
    # next export starts after it. This bounds memory by overlapping source
    # windows instead of retaining every row across the full warmup period.
    overlapping_sources: list[tuple[date, dict[bytes, bytes]]] = []
    for path in sorted((raw_dir / "bts").glob("*.zip")):
        source_seen: dict[bytes, bytes] = {}
        source_min: date | None = None
        source_max: date | None = None
        try:
            with zipfile.ZipFile(path) as archive:
                member = _csv_member(archive, path)
                with (
                    archive.open(member) as raw_stream,
                    io.TextIOWrapper(
                        raw_stream, encoding="utf-8-sig", errors="replace", newline=""
                    ) as stream,
                ):
                    reader = csv.DictReader(stream)
                    missing = BTS_REQUIRED - set(reader.fieldnames or [])
                    if missing:
                        raise DataPipelineError(
                            f"BTS schema is missing columns: {', '.join(sorted(missing))}"
                        )
                    for row in reader:
                        try:
                            flight_day = date.fromisoformat(row["FlightDate"][:10])
                        except (ValueError, TypeError) as exc:
                            raise DataPipelineError("BTS contains invalid FlightDate") from exc
                        if start <= flight_day <= end:
                            source_min = min(source_min, flight_day) if source_min else flight_day
                            source_max = max(source_max, flight_day) if source_max else flight_day
                            key, fingerprint = _bts_row_digests(row)
                            previous = source_seen.get(key)
                            if previous is None:
                                for _latest_date, prior_source in overlapping_sources:
                                    previous = prior_source.get(key)
                                    if previous is not None:
                                        break
                            if previous is not None:
                                if previous != fingerprint:
                                    raise DataPipelineError(
                                        "conflicting BTS records share flight identity "
                                        f"{_bts_business_key(row)!r}; refresh the source "
                                        "files before building"
                                    )
                                if quality_counts is not None:
                                    quality_counts["duplicate_bts_rows_removed"] += 1
                            else:
                                yield row
                            source_seen[key] = fingerprint
            if source_max is not None and source_min is not None:
                overlapping_sources = [
                    (latest_date, digest_map)
                    for latest_date, digest_map in overlapping_sources
                    if latest_date >= source_min
                ]
                overlapping_sources.append((source_max, source_seen))
        except zipfile.BadZipFile as exc:
            raise DataPipelineError(f"invalid BTS ZIP file: {path}") from exc


def _weather(
    raw_dir: Path, stations: dict[str, str], zones: dict[str, str], years: set[int]
) -> dict[str, list[dict[str, Any]]]:
    weather: dict[str, list[dict[str, Any]]] = defaultdict(list)
    station_timezone = {station: zones[airport] for airport, station in stations.items()}
    for station in sorted(set(stations.values())):
        for year in sorted(years):
            path = raw_dir / "lcd" / str(year) / f"{station}.csv"
            if not path.exists():
                raise DataPipelineError(f"LCD source missing: {path}; run fetch first")
            validate_lcd_csv(path)
            with path.open(encoding="utf-8-sig", newline="") as stream:
                parsed = load_ncei_csv(stream, timezone_name=station_timezone[station])
            weather[station].extend(parsed)
    for station in weather:
        weather[station].sort(key=lambda obs: obs["ts"])
    return weather


def build_dataset(
    *,
    raw_dir: Path,
    output_dir: Path,
    start: date,
    end: date,
    timezone_path: Path | None,
    station_mapping_path: Path,
    carrier_mapping_path: Path | None = None,
    top_airports: int,
    airport_list: list[str] | None = None,
    weather_window_hours: int = 3,
) -> dict[str, Any]:
    if output_dir.exists():
        raise DataPipelineError(f"output already exists; choose a new path: {output_dir}")
    if top_airports < 1 or weather_window_hours < 0:
        raise ValueError("top_airports must be positive and weather window nonnegative")
    zones, stations = load_reference_tables(timezone_path, station_mapping_path)
    carrier_reference = load_carrier_reference(carrier_mapping_path)
    warmup_start = start - timedelta(days=365)
    origin_counts: Counter[str] = Counter()
    data_quality_counts: Counter[str] = Counter()
    for row in _iter_bts_rows(raw_dir, warmup_start, end, quality_counts=data_quality_counts):
        flight_day = date.fromisoformat(row["FlightDate"][:10])
        if start <= flight_day <= end:
            origin_counts[row["Origin"].strip().upper()] += 1
    if not origin_counts:
        raise DataPipelineError("no BTS rows found within configured date range")
    selected = (
        {airport.upper() for airport in airport_list}
        if airport_list
        else {airport for airport, _count in origin_counts.most_common(top_airports)}
    )
    if not selected <= set(zones):
        raise DataPipelineError(
            f"selected airports lack timezone mappings: {sorted(selected - set(zones))}"
        )
    if not selected <= set(stations):
        raise DataPipelineError(
            "selected airports lack explicit LCD station mappings: "
            f"{sorted(selected - set(stations))}"
        )
    weather = _weather(
        raw_dir,
        {a: stations[a] for a in selected},
        zones,
        # Weather is joined only to emitted rows; prior BTS rows seed schedule
        # history but do not require historical weather observations.
        set(range(start.year, end.year + 1)),
    )
    normalized: list[dict[str, Any]] = []
    for row in _iter_bts_rows(raw_dir, warmup_start, end):
        origin = row["Origin"].strip().upper()
        if origin not in selected:
            continue
        dest = row["Dest"].strip().upper()
        if origin not in zones:
            raise DataPipelineError(f"flight origin has no timezone mapping: {origin}")
        day = date.fromisoformat(row["FlightDate"][:10])
        dep_local_naive = _scheduled_local(row["CRSDepTime"], day, field="departure")
        dep_time = dep_local_naive.time()
        dep_local = dep_local_naive.replace(tzinfo=ZoneInfo(zones[origin]))
        dep_utc = dep_local.astimezone(UTC)
        scheduled_arrival_utc = None
        if dest in zones:
            arr_local = _scheduled_local(row["CRSArrTime"], day, field="arrival")
            scheduled_arrival_utc = arr_local.replace(tzinfo=ZoneInfo(zones[dest])).astimezone(UTC)
            while scheduled_arrival_utc <= dep_utc:
                arr_local += timedelta(days=1)
                scheduled_arrival_utc = arr_local.replace(tzinfo=ZoneInfo(zones[dest])).astimezone(
                    UTC
                )
        delay = _binary_bts_value(row["ArrDel15"], field="ArrDel15", allow_blank=True)
        cancelled = _binary_bts_value(row["Cancelled"], field="Cancelled")
        normalized.append(
            {
                "carrier": row["Reporting_Airline"].strip().upper(),
                "origin": origin,
                "dest": dest,
                "flight_no": row["Flight_Number_Reporting_Airline"].strip(),
                "flight_date": day,
                "crs_dep_time": dep_time,
                "dep_utc": dep_utc,
                "ArrDel15": delay,
                "Cancelled": cancelled,
                "ontime": int(delay == 0 and cancelled == 0),
                "tail_number": row.get("Tail_Number", "").strip(),
                "arr_delay": float(row.get("ArrDelay", "").strip() or "0"),
                "scheduled_arrival_utc": scheduled_arrival_utc,
            }
        )
    normalized.sort(key=lambda item: (item["dep_utc"], item["carrier"], item["flight_no"]))
    if not normalized:
        raise DataPipelineError("no BTS rows have observed binary labels")
    history: dict[str, list[tuple[datetime, int, int | None]]] = defaultdict(list)
    carrier_history: dict[str, list[tuple[datetime, int, int | None]]] = defaultdict(list)
    history_sum: dict[str, int] = defaultdict(int)
    history_count: dict[str, int] = defaultdict(int)
    carrier_sum: dict[str, int] = defaultdict(int)
    carrier_count: dict[str, int] = defaultdict(int)
    pending_outcomes: list[tuple[str, str, datetime, datetime, int | None]] = []
    pending_timestamp: datetime | None = None
    available_outcomes: list[tuple[datetime, datetime, int, str, str, int | None]] = []
    outcome_order = 0
    tail_history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    weather_cursor: dict[str, int] = defaultdict(int)
    features: list[dict[str, Any]] = []
    floor_days = timedelta(days=365)
    for flight in normalized:
        day, dep_utc = flight["flight_date"], flight["dep_utc"]
        if pending_timestamp is not None and dep_utc != pending_timestamp:
            for route_key, carrier_key, departure, available_at, outcome in pending_outcomes:
                heapq.heappush(
                    available_outcomes,
                    (available_at, departure, outcome_order, route_key, carrier_key, outcome),
                )
                outcome_order += 1
            pending_outcomes.clear()
        # A label enters historical rates only once its result could have been
        # observed. Keep the departure time for the rolling-window boundary.
        while available_outcomes and available_outcomes[0][0] <= dep_utc:
            _available_at, departure, order, route_key, carrier_key, outcome = heapq.heappop(
                available_outcomes
            )
            if departure >= dep_utc:
                # This also keeps equal-departure flights out of one another's
                # features, including cancellation labels available at departure.
                heapq.heappush(
                    available_outcomes,
                    (_available_at, departure, order, route_key, carrier_key, outcome),
                )
                break
            heapq.heappush(history[route_key], (departure, order, outcome))
            heapq.heappush(carrier_history[carrier_key], (departure, order, outcome))
            if outcome is not None:
                history_sum[route_key] += outcome
                history_count[route_key] += 1
                carrier_sum[carrier_key] += outcome
                carrier_count[carrier_key] += 1
        pending_timestamp = dep_utc
        route = f"{flight['origin']}|{flight['dest']}"
        carrier_origin = f"{flight['carrier']}|{flight['origin']}"
        route_hist, carrier_hist = history[route], carrier_history[carrier_origin]
        while route_hist and (dep_utc - route_hist[0][0] > floor_days):
            _timestamp, _order, expired = heapq.heappop(route_hist)
            if expired is not None:
                history_sum[route] -= expired
                history_count[route] -= 1
        while carrier_hist and (dep_utc - carrier_hist[0][0] > floor_days):
            _timestamp, _order, expired = heapq.heappop(carrier_hist)
            if expired is not None:
                carrier_sum[carrier_origin] -= expired
                carrier_count[carrier_origin] -= 1
        # Outcomes at equal departure instants remain pending until the timestamp
        # changes, preventing target leakage while keeping rolling rates O(1).
        route_rate = history_sum[route] / history_count[route] if history_count[route] else 0.8
        carrier_rate = (
            carrier_sum[carrier_origin] / carrier_count[carrier_origin]
            if carrier_count[carrier_origin]
            else 0.8
        )
        tail = flight["tail_number"]
        completed_turns = tail_history[tail]
        completed_turns[:] = [
            turn for turn in completed_turns if turn[0] >= dep_utc - timedelta(hours=24)
        ]
        turn_delay_proxy = max(
            (delay for arrival, delay in completed_turns if arrival < dep_utc), default=0.0
        )
        if tail and flight["Cancelled"] == 0 and flight["scheduled_arrival_utc"] is not None:
            actual_arrival = flight["scheduled_arrival_utc"] + timedelta(
                minutes=flight["arr_delay"]
            )
            tail_history[tail].append((actual_arrival, flight["arr_delay"]))
        observed_ontime = flight["ontime"] if flight["ArrDel15"] is not None else None
        outcome_available_at = (
            flight["scheduled_arrival_utc"] + timedelta(minutes=flight["arr_delay"])
                if flight["Cancelled"] == 0
                and flight["ArrDel15"] is not None
                and flight["scheduled_arrival_utc"] is not None
                else datetime.max.replace(tzinfo=UTC)
                if flight["Cancelled"] == 0 and flight["ArrDel15"] is not None
            else dep_utc
        )
        pending_outcomes.append(
            (route, carrier_origin, dep_utc, outcome_available_at, observed_ontime)
        )
        if day < start:
            continue
        station = stations[flight["origin"]]
        observations = weather[station]
        cursor = weather_cursor[station]
        while cursor < len(observations) and observations[cursor]["ts"] <= dep_utc:
            cursor += 1
        weather_cursor[station] = cursor
        recent = observations[cursor - 1] if cursor else None
        if not recent or dep_utc - recent["ts"] > timedelta(hours=weather_window_hours):
            raise DataPipelineError(
                f"missing/stale LCD observation for {flight['origin']} station {station} "
                f"at {dep_utc.isoformat()} (maximum age {weather_window_hours}h)"
            )
        adverse = bool(
            (recent.get("ceiling_ft") is not None and recent["ceiling_ft"] < 1000)
            or (recent.get("visibility_mi") is not None and recent["visibility_mi"] < 3)
            or any(
                code in {"TS", "TSRA", "SN", "FZRA", "FG"} for code in recent.get("wx_codes", ())
            )
        )
        if start <= day <= end:
            features.append(
                {
                    "carrier": flight["carrier"],
                    "flight_no": flight["flight_no"],
                    "origin": flight["origin"],
                    "dest": flight["dest"],
                    "flight_date": day.isoformat(),
                    "crs_dep_time": flight["crs_dep_time"].isoformat(),
                    "prediction_timestamp_utc": dep_utc.isoformat(),
                    "route_historical_ontime_rate": route_rate,
                    "carrier_origin_historical_rate": carrier_rate,
                    "aircraft_turn_delay_proxy": max(0.0, turn_delay_proxy),
                    "adverse_weather": int(adverse),
                    "ArrDel15": flight["ArrDel15"],
                    "Cancelled": flight["Cancelled"],
                    "weather_station_id": station,
                    "weather_observation_utc": recent["ts"].isoformat() if recent else "",
                    "year": day.year,
                    "month": day.month,
                }
            )
    # Prediction timestamps are globally ordered in UTC; local service dates can
    # move backward at the date line when several origins are combined.
    features.sort(
        key=lambda item: (
            item["prediction_timestamp_utc"],
            item["origin"],
            item["carrier"],
        )
    )
    try:
        import duckdb
        import pyarrow as pa  # type: ignore[import-untyped]
        import pyarrow.dataset as ds  # type: ignore[import-untyped]
    except ImportError as exc:
        raise DataPipelineError("Parquet + DuckDB output requires `uv sync --extra data`") from exc
    output_dir.mkdir(parents=True)
    parquet_root = output_dir / "flights"
    table = pa.Table.from_pylist(features)
    ds.write_dataset(
        table, base_dir=str(parquet_root), format="parquet", partitioning=["year", "month"]
    )
    training_csv = output_dir / "training.csv"
    with training_csv.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=DATASET_COLUMNS)
        writer.writeheader()
        for item in features:
            writer.writerow({key: item[key] for key in DATASET_COLUMNS})
    database = output_dir / "analytics.duckdb"
    connection = duckdb.connect(str(database))
    try:
        glob = str(parquet_root / "**" / "*.parquet").replace("'", "''")
        connection.execute(
            f"CREATE TABLE flights AS SELECT * FROM read_parquet('{glob}', hive_partitioning=true)"
        )
        connection.execute("CREATE INDEX flights_date_origin_idx ON flights(flight_date, origin)")
    finally:
        connection.close()
    source_records = _source_manifest(raw_dir)
    source_snapshot_sha256 = _source_snapshot_sha256(source_records)
    quality_checks = [
        {
            "name": "unique_flight_identity",
            "sql": (
                "SELECT count(*) = count(DISTINCT (flight_date, origin, dest, carrier, "
                "flight_no, crs_dep_time)) FROM flights"
            ),
            "passed": connection_query(
                database,
                "SELECT count(*) = count(DISTINCT (flight_date, origin, dest, carrier, "
                "flight_no, crs_dep_time)) AS passed FROM flights",
            )[0]["passed"],
        },
        {
            "name": "required_feature_values_present",
            "sql": (
                "SELECT count(*) = 0 FROM flights WHERE origin IS NULL OR dest IS NULL "
                "OR carrier IS NULL OR prediction_timestamp_utc IS NULL"
            ),
            "passed": connection_query(
                database,
                "SELECT count(*) = 0 AS passed FROM flights WHERE origin IS NULL "
                "OR dest IS NULL OR carrier IS NULL OR prediction_timestamp_utc IS NULL",
            )[0]["passed"],
        },
    ]
    if not all(check["passed"] for check in quality_checks):
        raise DataPipelineError("materialized dataset failed SQL data-quality checks")
    built_at = datetime.now(UTC).isoformat()
    manifest = {
        "pipeline_version": PIPELINE_VERSION,
        "created_at_utc": built_at,
        "freshness": {
            "status": "current_at_materialization",
            "checked_at_utc": built_at,
            "source_snapshot_sha256": source_snapshot_sha256,
            "note": "Local source snapshot only; upstream publication freshness is not checked.",
        },
        "lineage": {
            "source_snapshot_sha256": source_snapshot_sha256,
            "sources": source_records,
            "materialization": "full requested-window rebuild",
        },
        "sources": source_records,
        "filters": {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "selected_origin_airports": sorted(selected),
            "airport_selection": "explicit"
            if airport_list
            else f"top {top_airports} by BTS departure count",
            "top_airports": top_airports,
            "weather_window_hours": weather_window_hours,
        },
        "carrier_reference": {
            "path": str(
                carrier_mapping_path
                or Path(__file__).parents[2] / "config" / "carrier_reference.csv"
            ),
            "sha256": sha256_file(
                carrier_mapping_path
                or Path(__file__).parents[2] / "config" / "carrier_reference.csv"
            ),
            "unknown_codes_in_filtered_data": sorted(
                {item["carrier"] for item in normalized if item["flight_date"] >= start}
                - set(carrier_reference)
            ),
        },
        "weather": {
            "join": "latest LCD hourly observation at or before scheduled departure UTC",
            "maximum_age_hours": weather_window_hours,
            "missing_policy": "fail dataset build",
            "station_mapping_sha256": sha256_file(station_mapping_path),
            "airport_station_mapping": [
                {
                    "airport": airport,
                    "station_id": stations[airport],
                    "ncei_station_metadata_url": (
                        "https://www.ncei.noaa.gov/cdo-web/datasets/LCD/stations/"
                        f"WBAN%3A{stations[airport][-5:]}/detail"
                    ),
                }
                for airport in sorted(selected)
            ],
            "timezone_mapping_sha256": sha256_file(
                timezone_path or Path(__file__).parents[2] / "config" / "airport_timezones.csv"
            ),
        },
        "feature_columns": list(FEATURE_COLUMNS),
        "rows": len(features),
        "parquet_rows": len(features),
        "data_quality": {
            "checks": quality_checks,
            "duplicate_bts_rows_removed": data_quality_counts["duplicate_bts_rows_removed"],
            "conflicting_duplicate_keys": 0,
        },
        "resource_usage": {
            "peak_rss_bytes": _peak_rss_bytes(),
            "normalized_rows_retained_for_temporal_sort": len(normalized),
            "raw_bts_rows_retained": 0,
        },
        "training_csv_sha256": sha256_file(training_csv),
        "parquet_files": [
            {
                "path": str(path.relative_to(output_dir)),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in sorted(parquet_root.rglob("*.parquet"))
        ],
        "analytics_database": str(database.relative_to(output_dir)),
        "benchmark_query": (
            "SELECT origin, count(*) flights, avg(ArrDel15) delay_rate "
            "FROM flights GROUP BY origin ORDER BY flights DESC"
        ),
        "benchmark_query_result": connection_query(
            database,
            "SELECT origin, count(*) AS flights, avg(ArrDel15) AS delay_rate "
            "FROM flights GROUP BY origin ORDER BY flights DESC",
        ),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def connection_query(database: Path, query: str) -> list[dict[str, Any]]:
    import duckdb

    connection = duckdb.connect(str(database), read_only=True)
    try:
        result = connection.execute(query)
        columns = [item[0] for item in result.description]
        return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
    finally:
        connection.close()


def _source_manifest(raw_dir: Path) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for path in sorted(raw_dir.rglob("*")):
        if path.is_file() and (path.suffix.lower() == ".zip" or path.parent.name.isdigit()):
            if path.suffix.lower() == ".zip":
                month = path.stem
                year, month_no = (int(part) for part in month.split("-"))
                url = bts_month_url(year, month_no)
                kind = "bts_zip"
            else:
                year = int(path.parent.name)
                station = path.stem
                url = lcd_year_url(year, station)
                kind = "ncei_lcd_csv"
            sources.append(
                {
                    "kind": kind,
                    "url": url,
                    "path": str(path),
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    return sources


def _source_snapshot_sha256(sources: list[dict[str, Any]]) -> str:
    """Hash the ordered source identities and bytes represented by a build."""
    snapshot = [
        {"url": source["url"], "sha256": source["sha256"]}
        for source in sorted(sources, key=lambda item: str(item["url"]))
    ]
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _changed_sources(
    previous: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, str]]:
    before = {str(source["url"]): str(source["sha256"]) for source in previous}
    after = {str(source["url"]): str(source["sha256"]) for source in current}
    return [
        {"url": url, "previous_sha256": before.get(url, ""), "current_sha256": after.get(url, "")}
        for url in sorted(before.keys() | after.keys())
        if before.get(url) != after.get(url)
    ]


def _materialization_is_valid(output_dir: Path, manifest: dict[str, Any]) -> bool:
    """Check that the committed local outputs still match their recorded hashes."""
    training_csv = output_dir / "training.csv"
    database = output_dir / str(manifest.get("analytics_database", "analytics.duckdb"))
    if not training_csv.is_file() or not database.is_file():
        return False
    if sha256_file(training_csv) != manifest.get("training_csv_sha256"):
        return False
    parquet_records = manifest.get("parquet_files")
    if not isinstance(parquet_records, list) or not parquet_records:
        return False
    for record in parquet_records:
        path = output_dir / str(record.get("path", ""))
        if not path.is_file() or sha256_file(path) != record.get("sha256"):
            return False
    return True


def refresh_dataset(
    *,
    raw_dir: Path,
    output_dir: Path,
    start: date,
    end: date,
    timezone_path: Path | None,
    station_mapping_path: Path,
    carrier_mapping_path: Path | None = None,
    top_airports: int,
    airport_list: list[str] | None = None,
    weather_window_hours: int = 3,
) -> dict[str, Any]:
    """Refresh an existing local dataset idempotently from current local source bytes.

    The source feeds have temporal feature dependencies: changing a BTS month can
    affect later rolling history and a correction to LCD can affect joined weather.
    Therefore changed source bytes invalidate the complete requested output window.
    The new tree is built beside the existing tree and swapped into place only after
    every build and data-quality check succeeds.
    """
    source_records = _source_manifest(raw_dir)
    current_snapshot = _source_snapshot_sha256(source_records)
    timezone_file = timezone_path or Path(__file__).parents[2] / "config" / "airport_timezones.csv"
    carrier_file = (
        carrier_mapping_path or Path(__file__).parents[2] / "config" / "carrier_reference.csv"
    )
    current_references = {
        "timezone_mapping_sha256": sha256_file(timezone_file),
        "station_mapping_sha256": sha256_file(station_mapping_path),
        "carrier_mapping_sha256": sha256_file(carrier_file),
    }
    manifest_path = output_dir / "manifest.json"
    previous: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except json.JSONDecodeError:
            previous = {}
    changed = _changed_sources(previous.get("sources", []), source_records) if previous else []
    previous_weather = previous.get("weather", {})
    previous_carrier = previous.get("carrier_reference", {})
    previous_references = {
        "timezone_mapping_sha256": previous_weather.get("timezone_mapping_sha256"),
        "station_mapping_sha256": previous_weather.get("station_mapping_sha256"),
        "carrier_mapping_sha256": previous_carrier.get("sha256"),
    }
    changed_references = [
        {
            "name": name,
            "previous_sha256": str(previous_references[name] or ""),
            "current_sha256": digest,
        }
        for name, digest in current_references.items()
        if previous and previous_references[name] != digest
    ]
    requested_filters = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "selected_origin_airports": sorted({airport.upper() for airport in airport_list})
        if airport_list
        else None,
        "top_airports": top_airports,
        "weather_window_hours": weather_window_hours,
    }
    filters = previous.get("filters", {})
    same_filters = (
        previous.get("pipeline_version") == PIPELINE_VERSION
        and
        filters.get("start_date") == requested_filters["start_date"]
        and filters.get("end_date") == requested_filters["end_date"]
        and filters.get("weather_window_hours", weather_window_hours) == weather_window_hours
        and not changed_references
        and (
            filters.get("selected_origin_airports") == requested_filters["selected_origin_airports"]
            if airport_list
            else filters.get("airport_selection") == f"top {top_airports} by BTS departure count"
        )
    )
    if (
        previous
        and not changed
        and same_filters
        and _materialization_is_valid(output_dir, previous)
        and previous.get("freshness", {}).get("source_snapshot_sha256") == current_snapshot
    ):
        checked_at = datetime.now(UTC).isoformat()
        previous["freshness"] = {
            "status": "current_at_last_refresh_check",
            "checked_at_utc": checked_at,
            "source_snapshot_sha256": current_snapshot,
            "note": "Local source snapshot only; upstream publication freshness is not checked.",
        }
        previous["lineage"]["last_refresh"] = {
            "checked_at_utc": checked_at,
            "changed_sources": [],
            "changed_reference_files": [],
            "action": "no_op_sources_and_materialization_unchanged",
        }
        manifest_path.write_text(
            json.dumps(previous, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return previous

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-refresh-", dir=output_dir.parent)
    )
    staged_output = staging_root / "dataset"
    backup = output_dir.parent / f".{output_dir.name}-backup-{uuid.uuid4().hex}"
    previous_manifest_sha256 = sha256_file(manifest_path) if manifest_path.is_file() else None
    try:
        refreshed = build_dataset(
            raw_dir=raw_dir,
            output_dir=staged_output,
            start=start,
            end=end,
            timezone_path=timezone_path,
            station_mapping_path=station_mapping_path,
            carrier_mapping_path=carrier_mapping_path,
            top_airports=top_airports,
            airport_list=airport_list,
            weather_window_hours=weather_window_hours,
        )
        refreshed["freshness"]["status"] = (
            "refreshed_after_source_change"
            if changed or changed_references
            else "rebuilt_materialization"
        )
        refreshed["lineage"]["last_refresh"] = {
            "checked_at_utc": datetime.now(UTC).isoformat(),
            "previous_manifest_sha256": previous_manifest_sha256,
            "changed_sources": changed,
            "changed_reference_files": changed_references,
            "action": "full_requested_window_rebuild",
        }
        staged_manifest = staged_output / "manifest.json"
        staged_manifest.write_text(
            json.dumps(refreshed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if output_dir.exists():
            output_dir.replace(backup)
        try:
            staged_output.replace(output_dir)
        except Exception:
            if backup.exists() and not output_dir.exists():
                backup.replace(output_dir)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return refreshed
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def train_from_dataset(dataset_dir: Path, model_path: Path) -> dict[str, Any]:
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        raise DataPipelineError(f"dataset manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    training_csv = dataset_dir / "training.csv"
    if sha256_file(training_csv) != manifest.get("training_csv_sha256"):
        raise DataPipelineError("training CSV SHA-256 does not match dataset manifest")
    model = train_delay_risk_model(
        training_csv,
        training_data_provenance=(
            "BTS TranStats and NOAA NCEI LCD; dataset manifest "
            f"{manifest_path}; data SHA-256 {manifest['training_csv_sha256']}"
        ),
    )
    model.metadata["dataset_manifest_sha256"] = sha256_file(manifest_path)
    model.metadata["dataset_rows"] = manifest["rows"]
    model.metadata["dataset_filters"] = manifest["filters"]
    model.save(model_path)
    training_manifest = {
        "dataset_manifest": str(manifest_path),
        "dataset_manifest_sha256": sha256_file(manifest_path),
        "training_csv_sha256": manifest["training_csv_sha256"],
        "model_path": str(model_path),
        "model_sha256": sha256_file(model_path),
        "model_metadata": model.metadata,
        "metrics": model.metrics,
    }
    model_path.with_suffix(model_path.suffix + ".manifest.json").write_text(
        json.dumps(training_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return training_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a local BTS + NOAA LCD delay-model dataset."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser(
        "fetch", help="fetch and validate official monthly BTS and yearly NOAA LCD files"
    )
    fetch.add_argument("--start", type=date.fromisoformat, required=True)
    fetch.add_argument("--end", type=date.fromisoformat, required=True)
    fetch.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    fetch.add_argument(
        "--station-mapping", type=Path, default=Path("config/airport_weather_stations.csv")
    )
    fetch.add_argument("--checksums", type=Path)
    build = sub.add_parser(
        "build", help="normalize, filter, featurize, and materialize Parquet + DuckDB"
    )
    _build_args(build)
    refresh = sub.add_parser(
        "refresh",
        help="idempotently refresh a local dataset when source checksums or outputs change",
    )
    _build_args(refresh)
    train = sub.add_parser(
        "train", help="train the existing model and write metrics with provenance"
    )
    train.add_argument("--dataset-dir", type=Path, required=True)
    train.add_argument("--model", type=Path, required=True)
    all_cmd = sub.add_parser("all", help="fetch, build, and train")
    _build_args(all_cmd)
    all_cmd.add_argument("--model", type=Path, default=Path("models/delayrisk-real.json"))
    return parser


def _build_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timezone-map", type=Path, default=Path("config/airport_timezones.csv"))
    parser.add_argument(
        "--station-mapping", type=Path, default=Path("config/airport_weather_stations.csv")
    )
    parser.add_argument("--carrier-map", type=Path, default=Path("config/carrier_reference.csv"))
    parser.add_argument("--top-airports", type=int, default=10)
    parser.add_argument("--airports", help="comma-separated explicit origin airport codes")
    parser.add_argument("--weather-window-hours", type=int, default=3)


def _build_from_args(args: argparse.Namespace) -> dict[str, Any]:
    airport_list = (
        [item.strip().upper() for item in args.airports.split(",") if item.strip()]
        if args.airports
        else None
    )
    return build_dataset(
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        start=args.start,
        end=args.end,
        timezone_path=args.timezone_map,
        station_mapping_path=args.station_mapping,
        carrier_mapping_path=args.carrier_map,
        top_airports=args.top_airports,
        airport_list=airport_list,
        weather_window_hours=args.weather_window_hours,
    )


def _refresh_from_args(args: argparse.Namespace) -> dict[str, Any]:
    airport_list = (
        [item.strip().upper() for item in args.airports.split(",") if item.strip()]
        if args.airports
        else None
    )
    return refresh_dataset(
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        start=args.start,
        end=args.end,
        timezone_path=args.timezone_map,
        station_mapping_path=args.station_mapping,
        carrier_mapping_path=args.carrier_map,
        top_airports=args.top_airports,
        airport_list=airport_list,
        weather_window_hours=args.weather_window_hours,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "fetch":
            records = fetch_sources(
                start=args.start,
                end=args.end,
                raw_dir=args.raw_dir,
                mapping_path=args.station_mapping,
                checksums_path=args.checksums,
            )
            print(json.dumps([asdict(item) for item in records], indent=2))
        elif args.command == "build":
            print(json.dumps(_build_from_args(args), indent=2))
        elif args.command == "refresh":
            print(json.dumps(_refresh_from_args(args), indent=2))
        elif args.command == "train":
            print(json.dumps(train_from_dataset(args.dataset_dir, args.model), indent=2))
        else:
            fetch_sources(
                start=args.start,
                end=args.end,
                raw_dir=args.raw_dir,
                mapping_path=args.station_mapping,
            )
            manifest = _build_from_args(args)
            training_manifest = train_from_dataset(args.output_dir, args.model)
            print(json.dumps({"dataset": manifest, "training": training_manifest}, indent=2))
    except (DataPipelineError, OSError, ValueError) as exc:
        print(f"data pipeline error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
