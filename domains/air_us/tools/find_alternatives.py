"""Search historical BTS schedule rows for replayable route alternatives."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from core.contracts.tools import (
    AlternativeFlightOption,
    FindAlternativesRequest,
    FindAlternativesResponse,
    FlightLeg,
)
from domains.air_us.ingest.bts import BTSFlight

MIN_CONNECTION_MINUTES = 30
MAX_CONNECTION_MINUTES = 8 * 60


class AlternativesDataError(ValueError):
    """Raised if historical schedules cannot support a trustworthy comparison."""


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AlternativesDataError(f"{label} must include a timezone")
    return value.astimezone(UTC)


def _flight_no(carrier: str, number: str) -> str:
    value = number.strip()
    if value.upper().startswith(carrier.upper()):
        return value
    return f"{carrier}{value}"


def find_alternatives(
    request: FindAlternativesRequest,
    *,
    flights: Iterable[BTSFlight],
    max_options: int = 20,
) -> FindAlternativesResponse:
    """Find itineraries from a local, historical BTS schedule snapshot.

    Arrival deltas always use ``original_scheduled_arrival`` supplied with the
    disruption facts. BTS candidates are never used to infer the original flight.
    """

    if max_options < 1:
        raise ValueError("max_options must be positive")
    not_before = _utc(request.not_before, "not_before")
    selected = tuple(item for item in flights if item.flight_date == request.flight_date)
    original_arrival = _utc(request.original_scheduled_arrival, "original_scheduled_arrival")

    by_origin: dict[str, list[BTSFlight]] = defaultdict(list)
    # Candidate rows describe the schedule as it was published. Filtering on a
    # cancellation outcome would use information unavailable at decision time.
    for item in selected:
        by_origin[item.origin].append(item)
    for legs in by_origin.values():
        legs.sort(key=lambda item: (item.crs_dep, item.crs_arr, item.carrier, item.flight_no))

    itineraries: list[tuple[tuple[BTSFlight, ...], int]] = []

    def walk(
        airport: str,
        path: tuple[BTSFlight, ...],
        arrival: datetime,
        visited: frozenset[str],
    ) -> None:
        if airport == request.dest and path:
            itineraries.append(
                (
                    path,
                    0
                    if len(path) == 1
                    else int((path[-1].crs_dep - path[-2].crs_arr).total_seconds() // 60),
                )
            )
            return
        if len(path) >= request.max_stops + 1:
            return
        earliest = not_before if not path else arrival
        for candidate in by_origin.get(airport, []):
            if candidate.dest in visited or candidate.crs_dep < earliest:
                continue
            buffer = int((candidate.crs_dep - arrival).total_seconds() // 60) if path else 0
            if path and not MIN_CONNECTION_MINUTES <= buffer <= MAX_CONNECTION_MINUTES:
                continue
            if request.same_carrier_only and path and candidate.carrier != path[0].carrier:
                continue
            walk(candidate.dest, path + (candidate,), candidate.crs_arr, visited | {candidate.dest})

    walk(request.origin, (), not_before, frozenset({request.origin}))

    # Only outcomes observable by decision time are usable as reliability evidence.
    # Flown flights use scheduled arrival plus BTS ArrDelay. BTS has no cancellation
    # event timestamp, so cancellation labels cannot safely enter this history.
    candidate_items = {leg for path, _ in itineraries for leg in path}
    route_counts: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for item in selected:
        if item in candidate_items or item.cancelled:
            continue
        if item.arrival_delay_min is None:
            # Without ArrDelay, actual completion cannot be placed on the timeline.
            continue
        actual_arrival = item.crs_arr + timedelta(minutes=item.arrival_delay_min)
        if actual_arrival >= not_before:
            continue
        counts = route_counts[(item.carrier, item.origin, item.dest)]
        counts[1] += 1
        counts[0] += int(item.ontime)

    dedup: dict[tuple[tuple[str, str, str], ...], tuple[tuple[BTSFlight, ...], int]] = {}
    for path, connection_buffer in itineraries:
        key = tuple((leg.carrier, leg.flight_no, leg.origin) for leg in path)
        dedup.setdefault(key, (path, connection_buffer))
    ordered = sorted(
        dedup.values(),
        key=lambda value: (
            value[0][-1].crs_arr,
            len(value[0]),
            tuple(x.flight_no for x in value[0]),
        ),
    )[:max_options]

    options: list[AlternativeFlightOption] = []
    for path, connection_buffer in ordered:
        flight_legs = tuple(
            FlightLeg(
                carrier=item.carrier,
                flight_no=_flight_no(item.carrier, item.flight_no),
                origin=item.origin,
                dest=item.dest,
                crs_dep=item.crs_dep,
                crs_arr=item.crs_arr,
            )
            for item in path
        )
        duration = int((path[-1].crs_arr - path[0].crs_dep).total_seconds() // 60)
        rates = [
            route_counts[(item.carrier, item.origin, item.dest)][0]
            / route_counts[(item.carrier, item.origin, item.dest)][1]
            for item in path
            if route_counts[(item.carrier, item.origin, item.dest)][1]
        ]
        options.append(
            AlternativeFlightOption(
                legs=flight_legs,
                total_duration_min=duration,
                connection_buffer_min=connection_buffer,
                hist_ontime_rate=(sum(rates) / len(rates)) if rates else 0.0,
                arrival_delta_vs_original_min=int(
                    (path[-1].crs_arr - original_arrival).total_seconds() // 60
                ),
            )
        )
    return FindAlternativesResponse(
        options=tuple(options), searched_date=request.flight_date, schedule_source="historical_bts"
    )
