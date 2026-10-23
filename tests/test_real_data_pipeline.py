from __future__ import annotations

import csv
import io
import json
import zipfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pytest

from domains.air_us.data_pipeline import (
    DataPipelineError,
    _bts_row_digests,
    _iter_bts_rows,
    bts_month_url,
    build_dataset,
    load_reference_tables,
    load_sha256_expectations,
    train_from_dataset,
    validate_bts_csv,
    validate_lcd_csv,
)
from domains.air_us.model.pipeline import train_delay_risk_model


def test_bts_2400_is_next_day_midnight() -> None:
    from domains.air_us.data_pipeline import _binary_bts_value, _scheduled_local

    assert _scheduled_local("2400", date(2025, 1, 1), field="departure") == datetime(
        2025, 1, 2, 0, 0
    )
    with pytest.raises(DataPipelineError, match="invalid scheduled arrival"):
        _scheduled_local("2460", date(2025, 1, 1), field="arrival")
    assert _binary_bts_value("0.00", field="ArrDel15") == 0
    assert _binary_bts_value("1.00", field="Cancelled") == 1
    assert _binary_bts_value("", field="ArrDel15", allow_blank=True) is None


def test_official_source_url_shapes_and_bts_schema_validation(tmp_path: Path) -> None:
    assert bts_month_url(2025, 7).endswith(
        "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_2025_7.zip"
    )
    good = tmp_path / "good.csv"
    good.write_text(
        "FlightDate,Reporting_Airline,Flight_Number_Reporting_Airline,Origin,Dest,"
        "CRSDepTime,CRSArrTime,ArrDel15,Cancelled\n"
        "2025-01-01,AA,1,JFK,LAX,0800,1100,0,0\n",
        encoding="utf-8",
    )
    validate_bts_csv(good)
    bad = tmp_path / "bad.csv"
    bad.write_text("FlightDate,Origin\n2025-01-01,JFK\n", encoding="utf-8")
    with pytest.raises(DataPipelineError, match="missing columns"):
        validate_bts_csv(bad)


def test_checksum_expectation_file_rejects_malformed_hash(tmp_path: Path) -> None:
    checksums = tmp_path / "checksums.json"
    checksums.write_text(json.dumps({"https://example.test/data": "bad"}), encoding="utf-8")
    with pytest.raises(DataPipelineError, match="64-character"):
        load_sha256_expectations(checksums)


def test_bts_duplicate_rows_are_removed_and_conflicts_fail_closed(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    original = raw / "bts" / "2025-01.zip"
    _write_synthetic_bts_zip(original)
    duplicate = raw / "bts" / "2025-02.zip"
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(original.read_bytes())
    quality_counts: Counter[str] = Counter()

    rows = list(
        _iter_bts_rows(raw, date(2024, 12, 31), date(2025, 12, 31), quality_counts=quality_counts)
    )

    assert len(rows) == 25
    assert quality_counts["duplicate_bts_rows_removed"] == 25
    with zipfile.ZipFile(duplicate) as archive:
        source = archive.read("monthly.csv").decode("utf-8")
    with zipfile.ZipFile(duplicate, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("monthly.csv", source.replace(",0830,1100,0,0,", ",0830,1100,1,0,"))

    with pytest.raises(DataPipelineError, match="conflicting BTS records"):
        list(_iter_bts_rows(raw, date(2024, 12, 31), date(2025, 12, 31)))


def test_bts_dedupe_fingerprints_have_fixed_size_and_preserve_conflict_detection() -> None:
    row = {
        "FlightDate": "2025-01-01",
        "Reporting_Airline": "AA",
        "Flight_Number_Reporting_Airline": "1",
        "Origin": "JFK",
        "Dest": "LAX",
        "CRSDepTime": "0800",
        "ArrDel15": "0",
        "Cancelled": "0",
    }
    key_digest, fingerprint = _bts_row_digests(row)
    corrected = {**row, "ArrDel15": "1"}
    corrected_key, corrected_fingerprint = _bts_row_digests(corrected)

    assert len(key_digest) == len(fingerprint) == 32
    assert corrected_key == key_digest
    assert corrected_fingerprint != fingerprint


def test_lcd_schema_accepts_official_station_without_optional_ceiling(tmp_path: Path) -> None:
    source = tmp_path / "lcd.csv"
    source.write_text(
        "DATE,HourlyVisibility,HourlyWindSpeed,HourlyPresentWeatherType,HourlySkyConditions\n"
        "2025-01-01T00:00:00,10.0,5,,CLR\n",
        encoding="utf-8",
    )

    validate_lcd_csv(source)


def test_checked_in_airport_and_weather_references_cover_at_least_50_airports() -> None:
    config = Path(__file__).parents[1] / "config"
    zones, stations = load_reference_tables(
        config / "airport_timezones.csv", config / "airport_weather_stations.csv"
    )
    assert len(zones) == len(stations)
    assert len(zones) >= 50
    assert set(zones) == set(stations)


def test_delay_model_masks_cancelled_rows_with_missing_arrival_delay(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "model" / "bts_sample.csv"
    with fixture.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
        columns = list(rows[0])
    cancelled = next(row for row in rows if row["Cancelled"] == "1")
    cancelled["ArrDel15"] = ""
    source = tmp_path / "training.csv"
    with source.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    model = train_delay_risk_model(source, training_data_provenance="local fixture")

    assert model.metrics["delay"]["test"]["eligible_rows_total"] == 23
    assert model.metrics["delay"]["test"]["eligible_rows"] == 5
    assert model.metrics["cancel"]["test"]["eligible_rows"] == 5
    assert model.metrics["delay"]["test"]["target_definition"].startswith(
        "ArrDel15 on rows with observed"
    )
    assert model.metrics["cancel"]["test"]["target_definition"] == "Cancelled"


def _write_synthetic_bts_zip(
    path: Path, *, first_day_delay: str = "0", rows_override: list[list[str]] | None = None
) -> None:
    headers = [
        "FlightDate",
        "Reporting_Airline",
        "Flight_Number_Reporting_Airline",
        "Origin",
        "Dest",
        "CRSDepTime",
        "CRSArrTime",
        "ArrDel15",
        "Cancelled",
        "Tail_Number",
        "ArrDelay",
    ]
    # A delayed prior-year flight seeds Jan 1's trailing route rate.
    rows: list[list[str]] = [
        [
            "2024-12-31",
            "AA",
            "1234",
            "JFK",
            "LAX",
            "1730",
            "2000",
            "1",
            "0",
            "N123AA",
            "20",
        ]
    ]
    if rows_override is None:
        for month in range(1, 13):
            for half, dep_time, delay in ((1, "0830", "0"), (15, "1730", "1")):
                day = date(2025, month, half)
                cancelled = "1" if month % 4 == 0 and half == 15 else "0"
                row_delay = first_day_delay if month == 1 and half == 1 else delay
                rows.append(
                    [
                        day.isoformat(),
                        "AA",
                        f"{month:02}{half:02}",
                        "JFK",
                        "PNS" if month == 2 and half == 1 else "LAX",
                        dep_time,
                        "1100" if dep_time == "0830" else "2000",
                        "" if cancelled == "1" else row_delay,
                        cancelled,
                        "N123AA",
                        "" if cancelled == "1" else ("20" if row_delay == "1" else "0"),
                    ]
                )
    else:
        rows = rows_override
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("monthly.csv", output.getvalue())


def test_temporal_features_wait_for_arrival_outcomes_and_include_prior_tail_arrivals(
    tmp_path: Path,
) -> None:
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    raw, refs = tmp_path / "raw", tmp_path / "refs"
    rows = [
        ["2024-12-31", "AA", "1", "JFK", "SFO", "1900", "2100", "1", "0", "TAIL1", "30"],
        ["2025-01-01", "AA", "2", "JFK", "LAX", "0800", "1600", "0", "0", "TAIL2", "0"],
        ["2025-01-01", "AA", "3", "JFK", "LAX", "0900", "1700", "0", "0", "TAIL1", "0"],
    ]
    _write_synthetic_bts_zip(raw / "bts" / "2025-01.zip", rows_override=rows)
    _write_synthetic_lcd(
        raw / "lcd" / "2025" / "72503014732.csv", [date(2025, 1, 1)]
    )
    _write_synthetic_lcd(
        raw / "lcd" / "2024" / "72503014732.csv", [date(2024, 12, 31)]
    )
    refs.mkdir()
    timezone_map = refs / "timezones.csv"
    timezone_map.write_text(
        "airport,time_zone\nJFK,America/New_York\nLAX,America/Los_Angeles\nSFO,America/Los_Angeles\n",
        encoding="utf-8",
    )
    station_map = refs / "stations.csv"
    station_map.write_text("airport,station_id\nJFK,72503014732\n", encoding="utf-8")

    output = tmp_path / "dataset"
    build_dataset(
        raw_dir=raw,
        output_dir=output,
        start=date(2025, 1, 1),
        end=date(2025, 1, 1),
        timezone_path=timezone_map,
        station_mapping_path=station_map,
        top_airports=1,
        airport_list=["JFK"],
        weather_window_hours=3,
    )
    with (output / "training.csv").open(newline="", encoding="utf-8") as stream:
        training_rows = list(csv.DictReader(stream))
    later_overlap = next(row for row in training_rows if row["crs_dep_time"] == "09:00:00")
    assert float(later_overlap["route_historical_ontime_rate"]) == 0.8
    assert float(later_overlap["aircraft_turn_delay_proxy"]) == 30.0


def test_delayed_outcomes_still_expire_by_departure_time_at_365_days(
    tmp_path: Path,
) -> None:
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    raw, refs = tmp_path / "raw", tmp_path / "refs"
    _write_synthetic_bts_zip(
        raw / "bts" / "2025-01.zip",
        rows_override=[
            ["2025-01-01", "AA", "1", "JFK", "LAX", "0800", "0500", "1", "0", "", "20"],
            ["2025-01-01", "AA", "2", "JFK", "LAX", "0900", "1200", "0", "0", "", "0"],
        ],
    )
    _write_synthetic_bts_zip(
        raw / "bts" / "2026-01.zip",
        rows_override=[
            ["2026-01-01", "AA", "3", "JFK", "LAX", "0900", "1200", "0", "0", "", "0"]
        ],
    )
    _write_synthetic_lcd(
        raw / "lcd" / "2026" / "72503014732.csv", [date(2026, 1, 1)]
    )
    refs.mkdir()
    timezone_map = refs / "timezones.csv"
    timezone_map.write_text(
        "airport,time_zone\nJFK,America/New_York\nLAX,America/Los_Angeles\n", encoding="utf-8"
    )
    station_map = refs / "stations.csv"
    station_map.write_text("airport,station_id\nJFK,72503014732\n", encoding="utf-8")

    output = tmp_path / "dataset"
    build_dataset(
        raw_dir=raw,
        output_dir=output,
        start=date(2026, 1, 1),
        end=date(2026, 1, 1),
        timezone_path=timezone_map,
        station_mapping_path=station_map,
        top_airports=1,
        airport_list=["JFK"],
        weather_window_hours=3,
    )
    with (output / "training.csv").open(newline="", encoding="utf-8") as stream:
        training_rows = list(csv.DictReader(stream))
    assert len(training_rows) == 1
    # The delayed Jan 1 08:00 departure arrived after the 09:00 flight, so its
    # outcome became available later. It is nevertheless outside the 365-day
    # window when the Jan 1 2026 09:00 departure is scored.
    assert float(training_rows[0]["route_historical_ontime_rate"]) == 1.0


def _write_synthetic_lcd(path: Path, timezone_offset_dates: list[date]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "DATE",
                "HourlyCeilingHeight",
                "HourlyVisibility",
                "HourlyWindSpeed",
                "HourlyPresentWeatherType",
            ]
        )
        for day in timezone_offset_dates:
            for hour in range(24):
                writer.writerow(
                    [
                        datetime.combine(day, datetime.min.time()).replace(hour=hour).isoformat(),
                        "10000",
                        "10",
                        "5",
                        "M",
                    ]
                )


def test_fixture_build_materializes_parquet_duckdb_and_trains_with_both_targets(
    tmp_path: Path,
) -> None:
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    raw, refs = tmp_path / "raw", tmp_path / "refs"
    _write_synthetic_bts_zip(raw / "bts" / "2025-01.zip")
    seed_rows = _iter_bts_rows(raw, date(2024, 12, 30), date(2024, 12, 31))
    assert iter(seed_rows) is seed_rows
    assert next(seed_rows)["FlightDate"] == "2024-12-31"
    _write_synthetic_lcd(
        raw / "lcd" / "2025" / "72503014732.csv",
        [date(2025, month, day) for month in range(1, 13) for day in (1, 15)],
    )
    _write_synthetic_lcd(
        raw / "lcd" / "2024" / "72503014732.csv",
        [date(2024, 2, 1), date(2024, 12, 31)],
    )
    refs.mkdir()
    timezone_map = refs / "timezones.csv"
    timezone_map.write_text(
        "airport,time_zone\nJFK,America/New_York\nLAX,America/Los_Angeles\n", encoding="utf-8"
    )
    station_map = refs / "stations.csv"
    station_map.write_text("airport,station_id\nJFK,72503014732\n", encoding="utf-8")

    output = tmp_path / "dataset"
    manifest = build_dataset(
        raw_dir=raw,
        output_dir=output,
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
        timezone_path=timezone_map,
        station_mapping_path=station_map,
        top_airports=1,
        airport_list=["JFK"],
        weather_window_hours=3,
    )

    assert manifest["rows"] == 24
    assert manifest["sources"]
    assert manifest["benchmark_query_result"][0]["flights"] == 24
    assert list((output / "flights").rglob("*.parquet"))
    with (output / "training.csv").open(newline="", encoding="utf-8") as stream:
        training_rows = list(csv.DictReader(stream))
    assert all(row["prediction_timestamp_utc"].endswith("+00:00") for row in training_rows)
    assert all(row["flight_date"] >= "2025-01-01" for row in training_rows)
    assert any(row["dest"] == "PNS" for row in training_rows)
    jan_first = next(
        row
        for row in training_rows
        if row["flight_date"] == "2025-01-01" and row["crs_dep_time"] == "08:30:00"
    )
    assert float(jan_first["route_historical_ontime_rate"]) == 0.0
    may_first = next(row for row in training_rows if row["flight_date"] == "2025-05-01")
    # The cancelled Apr 15 schedule must not become a completed tail arrival.
    assert float(may_first["aircraft_turn_delay_proxy"]) == 0.0
    training_manifest = train_from_dataset(output, tmp_path / "model.json")
    model_metrics = training_manifest["metrics"]
    assert model_metrics["delay"]["test"]["eligible_rows"] > 0
    assert model_metrics["cancel"]["test"]["eligible_rows"] > 0
    assert model_metrics["cancel"]["test"]["target_definition"] == "Cancelled"


def test_refresh_is_idempotent_and_rebuilds_after_late_source_correction(
    tmp_path: Path,
) -> None:
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from domains.air_us.data_pipeline import refresh_dataset

    raw, refs = tmp_path / "raw", tmp_path / "refs"
    bts_path = raw / "bts" / "2025-01.zip"
    _write_synthetic_bts_zip(bts_path)
    _write_synthetic_lcd(
        raw / "lcd" / "2025" / "72503014732.csv",
        [date(2025, month, day) for month in range(1, 13) for day in (1, 15)],
    )
    _write_synthetic_lcd(
        raw / "lcd" / "2024" / "72503014732.csv",
        [date(2024, 2, 1), date(2024, 12, 31)],
    )
    refs.mkdir()
    timezone_map = refs / "timezones.csv"
    timezone_map.write_text(
        "airport,time_zone\nJFK,America/New_York\nLAX,America/Los_Angeles\n", encoding="utf-8"
    )
    station_map = refs / "stations.csv"
    station_map.write_text("airport,station_id\nJFK,72503014732\n", encoding="utf-8")
    output = tmp_path / "dataset"
    arguments = {
        "raw_dir": raw,
        "output_dir": output,
        "start": date(2025, 1, 1),
        "end": date(2025, 12, 31),
        "timezone_path": timezone_map,
        "station_mapping_path": station_map,
        "top_airports": 1,
        "airport_list": ["JFK"],
    }

    first = refresh_dataset(**arguments)
    second = refresh_dataset(**arguments)
    assert first["training_csv_sha256"] == second["training_csv_sha256"]
    assert second["lineage"]["last_refresh"]["action"] == (
        "no_op_sources_and_materialization_unchanged"
    )

    _write_synthetic_bts_zip(bts_path, first_day_delay="1")
    corrected = refresh_dataset(**arguments)
    assert corrected["training_csv_sha256"] != first["training_csv_sha256"]
    assert corrected["freshness"]["status"] == "refreshed_after_source_change"
    assert corrected["lineage"]["last_refresh"]["action"] == "full_requested_window_rebuild"
    assert corrected["lineage"]["last_refresh"]["changed_sources"]
