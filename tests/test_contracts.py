from datetime import UTC, date, datetime, time

import pytest
from pydantic import ValidationError

from core.contracts import DelayRiskRequest, SearchRulesResponse, WeatherContextRequest


def test_contracts_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        DelayRiskRequest(
            carrier="AA",
            origin="JFK",
            dest="LAX",
            flight_date=date(2026, 7, 1),
            crs_dep_time=time(9, 30),
            mode="replay",
            hidden_guess=True,
        )


def test_empty_posture_is_a_valid_checked_result() -> None:
    result = SearchRulesResponse(
        clauses=(),
        posture=(),
        posture_checked=True,
        as_of_date=date(2026, 7, 3),
        excluded_by_date=2,
        corpus_version="rules-test",
    )
    assert result.posture == ()
    assert result.posture_checked is True


def test_weather_window_must_move_forward() -> None:
    instant = datetime(2026, 7, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="window_end must be after"):
        WeatherContextRequest(
            airport="JFK",
            window_start=instant,
            window_end=instant,
            mode="historical",
        )
