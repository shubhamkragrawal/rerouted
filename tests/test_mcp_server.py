from datetime import UTC, date, datetime, time

import pytest
from mcp import Client

from core.contracts.tools import (
    DelayRiskResponse,
    FindAlternativesResponse,
    SearchRulesResponse,
    WeatherContextResponse,
)
from core.mcp.server import create_mcp_server


@pytest.mark.asyncio
async def test_all_four_contract_tools_are_callable_in_memory() -> None:
    handlers = {
        "delay_risk": lambda _: DelayRiskResponse(
            p_delay_15=0.2,
            p_cancel=0.01,
            conformal_interval=(0.1, 0.3),
            calibration_bin="0.0-0.5",
            top_features=(),
            plain_english="Fixture estimate",
            model_version="fixture-v1",
            feature_pipeline_version="fixture-v1",
        ),
        "search_rules": lambda request: SearchRulesResponse(
            clauses=(),
            posture=(),
            posture_checked=True,
            as_of_date=request.as_of_date,
            excluded_by_date=0,
            corpus_version="fixture-v1",
        ),
        "weather_context": lambda request: WeatherContextResponse(
            source="NCEI-LCD" if request.mode.value == "historical" else "NWS",
            observations=(),
            adverse_flags=(),
            confidence="observed" if request.mode.value == "historical" else "forecast",
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        "find_alternatives": lambda request: FindAlternativesResponse(
            options=(), searched_date=request.flight_date
        ),
    }
    server = create_mcp_server(handlers)

    async with Client(server) as client:
        listed = await client.list_tools()
        by_name = {item.name: item for item in listed.tools}
        assert set(by_name) == set(handlers)
        assert set(by_name["delay_risk"].input_schema["required"]) == {
            "carrier",
            "origin",
            "dest",
            "flight_date",
            "crs_dep_time",
            "mode",
        }
        assert set(by_name["find_alternatives"].input_schema["required"]) == {
            "origin",
            "dest",
            "not_before",
            "original_scheduled_arrival",
            "max_stops",
            "same_carrier_only",
            "flight_date",
        }

        delay = await client.call_tool(
            "delay_risk",
            {
                "carrier": "AA",
                "origin": "JFK",
                "dest": "LAX",
                "flight_date": date(2026, 1, 1).isoformat(),
                "crs_dep_time": time(8).isoformat(),
                "mode": "fixture",
            },
        )
        rules = await client.call_tool(
            "search_rules",
            {
                "query": "refund",
                "as_of_date": "2026-01-01",
                "doc_types": [],
                "k": 5,
            },
        )
        weather = await client.call_tool(
            "weather_context",
            {
                "airport": "JFK",
                "window_start": "2026-01-01T00:00:00Z",
                "window_end": "2026-01-01T01:00:00Z",
                "mode": "historical",
            },
        )
        alternatives = await client.call_tool(
            "find_alternatives",
            {
                "origin": "JFK",
                "dest": "LAX",
                "not_before": "2026-01-01T08:00:00-05:00",
                "original_scheduled_arrival": "2026-01-01T15:00:00-05:00",
                "max_stops": 1,
                "same_carrier_only": False,
                "flight_date": "2026-01-01",
            },
        )

    assert not delay.is_error
    assert delay.structured_content["model_version"] == "fixture-v1"
    assert not rules.is_error
    assert rules.structured_content["posture_checked"] is True
    assert not weather.is_error
    assert weather.structured_content["source"] == "NCEI-LCD"
    assert not alternatives.is_error
    assert alternatives.structured_content["schedule_source"] == "historical_bts"


def test_stdio_entrypoint_exposes_configured_mcp_server() -> None:
    from core.mcp.entrypoint import mcp

    assert mcp.name == "Rerouted"
