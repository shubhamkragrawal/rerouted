from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from core.contracts.tools import FindAlternativesRequest
from domains.air_us.ingest.bts import BTSFlight, BTSIngestError, load_bts_csv, parse_bts_rows
from domains.air_us.tools.find_alternatives import find_alternatives

FIXTURES = Path(__file__).parent / "fixtures"
AIRPORT_TZ = {"JFK": "America/New_York", "ORD": "America/Chicago", "LAX": "America/Los_Angeles"}
FLIGHT_DATE = date(2026, 3, 14)


def test_bts_parser_and_alternatives_build_direct_and_connection_options() -> None:
    flights = load_bts_csv(
        (FIXTURES / "bts" / "sample.csv").read_text(),
        flight_date=FLIGHT_DATE,
        airport_timezones=AIRPORT_TZ,
    )
    assert len(flights) == 6
    request = FindAlternativesRequest(
        origin="JFK",
        dest="LAX",
        not_before=datetime(2026, 3, 14, 12, tzinfo=UTC),
        original_scheduled_arrival=datetime(2026, 3, 14, 18, tzinfo=UTC),
        max_stops=1,
        same_carrier_only=False,
        flight_date=FLIGHT_DATE,
    )
    response = find_alternatives(request, flights=flights)
    assert response.schedule_source == "historical_bts"
    assert response.searched_date == FLIGHT_DATE
    assert len(response.options) == 6
    direct = next(option for option in response.options if len(option.legs) == 1)
    connection = next(
        option
        for option in response.options
        if len(option.legs) == 2 and option.legs[0].carrier == "AA"
    )
    assert direct.arrival_delta_vs_original_min == 0
    assert connection.connection_buffer_min == 60
    assert connection.arrival_delta_vs_original_min == 150
    # None of the rows completed before the noon-UTC decision timestamp.
    assert connection.hist_ontime_rate == 0.0
    # The canceled historical schedule is still enumerated from scheduled times.
    assert any(option.legs[0].flight_no == "AA600" for option in response.options)


def test_candidates_use_published_schedule_and_reliability_uses_only_prior_outcomes() -> None:
    flights = (
        BTSFlight(
            "AA", "10", "JFK", "LAX", FLIGHT_DATE,
            datetime(2026, 3, 14, 8, tzinfo=UTC),
            datetime(2026, 3, 14, 10, tzinfo=UTC), True, False, 0,
        ),
        # Scheduled arrival is before noon, but actual arrival is after it.
        # Its failure label is not observable at the noon decision time.
        BTSFlight(
            "AA", "9", "JFK", "LAX", FLIGHT_DATE,
            datetime(2026, 3, 14, 9, tzinfo=UTC),
            datetime(2026, 3, 14, 11, 55, tzinfo=UTC), False, False, 10,
        ),
        # BTS has no cancellation-event timestamp, so this label is not usable
        # for reliability even though its scheduled departure precedes noon.
        BTSFlight(
            "AA", "8", "JFK", "LAX", FLIGHT_DATE,
            datetime(2026, 3, 14, 11, tzinfo=UTC),
            datetime(2026, 3, 14, 13, tzinfo=UTC), False, True,
        ),
        # The cancellation outcome is not known at the decision time, so this
        # published direct schedule remains a candidate.
        BTSFlight(
            "AA", "11", "JFK", "LAX", FLIGHT_DATE,
            datetime(2026, 3, 14, 14, tzinfo=UTC),
            datetime(2026, 3, 14, 16, tzinfo=UTC), False, True,
        ),
        # This completed outcome is later than the decision and must not leak.
        BTSFlight(
            "AA", "12", "JFK", "LAX", FLIGHT_DATE,
            datetime(2026, 3, 14, 15, tzinfo=UTC),
            datetime(2026, 3, 14, 17, tzinfo=UTC), False, False,
        ),
    )
    request = FindAlternativesRequest(
        origin="JFK",
        dest="LAX",
        not_before=datetime(2026, 3, 14, 12, tzinfo=UTC),
        original_scheduled_arrival=datetime(2026, 3, 14, 17, tzinfo=UTC),
        max_stops=0,
        same_carrier_only=False,
        flight_date=FLIGHT_DATE,
    )

    result = find_alternatives(request, flights=flights)

    assert [option.legs[0].flight_no for option in result.options] == ["AA11", "AA12"]
    # The delayed flight's scheduled arrival precedes noon, but its actual
    # arrival is 12:05 and cannot enter reliability. The cancellation has no
    # observable event timestamp and is excluded; future candidates do not enter.
    assert all(option.hist_ontime_rate == 1.0 for option in result.options)


def test_same_carrier_filter_and_stop_limit_are_applied() -> None:
    flights = load_bts_csv(
        (FIXTURES / "bts" / "sample.csv").read_text(),
        flight_date=FLIGHT_DATE,
        airport_timezones=AIRPORT_TZ,
    )
    request = FindAlternativesRequest(
        origin="JFK",
        dest="LAX",
        not_before=datetime(2026, 3, 14, 12, tzinfo=UTC),
        original_scheduled_arrival=datetime(2026, 3, 14, 18, tzinfo=UTC),
        max_stops=1,
        same_carrier_only=True,
        flight_date=FLIGHT_DATE,
    )
    response = find_alternatives(request, flights=flights)
    assert all(len({leg.carrier for leg in option.legs}) == 1 for option in response.options)
    assert not any(
        option.legs[0].carrier == "AA" and len(option.legs) == 2 and option.legs[-1].carrier == "UA"
        for option in response.options
    )
    direct_only = find_alternatives(request.model_copy(update={"max_stops": 0}), flights=flights)
    assert all(len(option.legs) == 1 for option in direct_only.options)


def test_bts_parser_fails_closed_for_missing_timezone() -> None:
    with pytest.raises(BTSIngestError, match="missing IANA time zone"):
        load_bts_csv(
            (FIXTURES / "bts" / "sample.csv").read_text(),
            flight_date=FLIGHT_DATE,
            airport_timezones={"JFK": "America/New_York"},
        )


def test_bts_parser_respects_cross_timezone_same_day_arrival() -> None:
    flights = parse_bts_rows(
        [
            {
                "FlightDate": "2026-03-14",
                "Reporting_Airline": "AA",
                "Flight_Number_Reporting_Airline": "101",
                "Origin": "JFK",
                "Dest": "LAX",
                "CRSDepTime": "2000",
                "CRSArrTime": "1800",
                "ArrDelay": "15",
                "ArrDel15": "0",
                "Cancelled": "0",
            }
        ],
        flight_date=FLIGHT_DATE,
        airport_timezones=AIRPORT_TZ,
    )
    assert flights[0].crs_arr == datetime(2026, 3, 15, 1, tzinfo=UTC)
    assert flights[0].arrival_delay_min == 15


def test_arrival_delta_uses_supplied_original_arrival_without_candidate_baseline() -> None:
    flights = load_bts_csv(
        (FIXTURES / "bts" / "sample.csv").read_text(),
        flight_date=FLIGHT_DATE,
        airport_timezones=AIRPORT_TZ,
    )
    request = FindAlternativesRequest(
        origin="JFK",
        dest="LAX",
        not_before=datetime(2026, 3, 14, 12, tzinfo=UTC),
        original_scheduled_arrival=datetime(2026, 3, 14, 17, tzinfo=UTC),
        max_stops=1,
        same_carrier_only=False,
        flight_date=FLIGHT_DATE,
    )
    no_direct = tuple(item for item in flights if item.flight_no != "100")
    response = find_alternatives(request, flights=no_direct)
    aa_connection = next(
        option
        for option in response.options
        if len(option.legs) == 2 and option.legs[0].carrier == "AA"
    )
    assert aa_connection.arrival_delta_vs_original_min == 210


def test_find_alternatives_requires_aware_original_arrival() -> None:
    with pytest.raises(ValueError, match="original_scheduled_arrival must include a timezone"):
        FindAlternativesRequest(
            origin="JFK",
            dest="LAX",
            not_before=datetime(2026, 3, 14, 12, tzinfo=UTC),
            original_scheduled_arrival=datetime(2026, 3, 14, 18),
            max_stops=1,
            same_carrier_only=False,
            flight_date=FLIGHT_DATE,
        )
