"""Official MCP Python SDK v2 server for the four locked air-travel tools."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time
from typing import Annotated, cast

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from core.authority import Authority
from core.contracts.tools import (
    DelayRiskRequest,
    DelayRiskResponse,
    FindAlternativesRequest,
    FindAlternativesResponse,
    SearchRulesRequest,
    SearchRulesResponse,
    WeatherContextRequest,
    WeatherContextResponse,
    WeatherMode,
)

ToolHandler = Callable[[object], object]


def create_mcp_server(handlers: dict[str, ToolHandler]) -> MCPServer:
    """Create a v2 MCPServer whose top-level input schemas mirror tool contracts.

    The SDK derives each JSON Schema from the typed Python arguments. Each handler
    then reconstructs the public Pydantic request, preserving contract validation.
    """

    missing = {
        "delay_risk",
        "search_rules",
        "weather_context",
        "find_alternatives",
    } - set(handlers)
    if missing:
        raise ValueError(f"MCP tool handlers missing: {', '.join(sorted(missing))}")

    server = MCPServer(
        "Rerouted",
        title="Rerouted flight-disruption tools",
        description="Local, source-grounded tools for US flight disruptions.",
        instructions=(
            "These tools explain published sources. They do not contact airlines or book flights. "
            "Historical alternatives are BTS schedule replays, not live inventory."
        ),
        version="0.1.0",
    )
    annotations = ToolAnnotations(
        read_only_hint=True,
        open_world_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
    )

    @server.tool(title="Estimate flight delay risk", annotations=annotations)
    def delay_risk(
        carrier: Annotated[str, Field(min_length=2, max_length=3, pattern=r"^[A-Z0-9]+$")],
        origin: Annotated[str, Field(pattern=r"^[A-Z]{3}$")],
        dest: Annotated[str, Field(pattern=r"^[A-Z]{3}$")],
        flight_date: date,
        crs_dep_time: time,
        mode: Annotated[str, Field(min_length=1)],
    ) -> DelayRiskResponse:
        """Estimate calibrated delay and cancellation probability for a flight."""

        request = DelayRiskRequest(
            carrier=carrier,
            origin=origin,
            dest=dest,
            flight_date=flight_date,
            crs_dep_time=crs_dep_time,
            mode=mode,
        )
        handler = cast(Callable[[DelayRiskRequest], DelayRiskResponse], handlers["delay_risk"])
        return handler(request)

    @server.tool(title="Search applicable rules", annotations=annotations)
    def search_rules(
        query: Annotated[str, Field(min_length=1)],
        as_of_date: date,
        doc_types: tuple[Authority, ...],
        k: Annotated[int, Field(ge=1, le=100)],
        carrier: Annotated[
            str | None,
            Field(min_length=2, max_length=3, pattern=r"^[A-Z0-9]+$"),
        ] = None,
    ) -> SearchRulesResponse:
        """Search date-applicable rules and the separately reviewed posture layer."""

        request = SearchRulesRequest(
            query=query,
            as_of_date=as_of_date,
            carrier=carrier,
            doc_types=doc_types,
            k=k,
        )
        handler = cast(
            Callable[[SearchRulesRequest], SearchRulesResponse], handlers["search_rules"]
        )
        return handler(request)

    @server.tool(title="Get weather context", annotations=annotations)
    def weather_context(
        airport: Annotated[str, Field(pattern=r"^[A-Z]{3}$")],
        window_start: datetime,
        window_end: datetime,
        mode: WeatherMode,
    ) -> WeatherContextResponse:
        """Return forecast from NWS or observations from historical NCEI LCD."""

        request = WeatherContextRequest(
            airport=airport,
            window_start=window_start,
            window_end=window_end,
            mode=mode,
        )
        handler = cast(
            Callable[[WeatherContextRequest], WeatherContextResponse],
            handlers["weather_context"],
        )
        return handler(request)

    @server.tool(title="Find historical flight alternatives", annotations=annotations)
    def find_alternatives(
        origin: Annotated[str, Field(pattern=r"^[A-Z]{3}$")],
        dest: Annotated[str, Field(pattern=r"^[A-Z]{3}$")],
        not_before: datetime,
        original_scheduled_arrival: datetime,
        max_stops: Annotated[int, Field(ge=0, le=3)],
        same_carrier_only: bool,
        flight_date: date,
    ) -> FindAlternativesResponse:
        """Find replayable alternatives in the historical BTS schedule."""

        request = FindAlternativesRequest(
            origin=origin,
            dest=dest,
            not_before=not_before,
            original_scheduled_arrival=original_scheduled_arrival,
            max_stops=max_stops,
            same_carrier_only=same_carrier_only,
            flight_date=flight_date,
        )
        handler = cast(
            Callable[[FindAlternativesRequest], FindAlternativesResponse],
            handlers["find_alternatives"],
        )
        return handler(request)

    return server
