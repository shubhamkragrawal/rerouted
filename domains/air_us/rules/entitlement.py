"""Deterministic predicates for 14 CFR Part 260 fare refunds.

This is a product rules engine, not legal advice.  The cited corpus remains the
source of truth; these predicates make its decision boundary executable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from core.resolver.models import EnforcementAssessment, EntitlementDecision, EntitlementStatus

from .posture import PostureRegistry, load_default_posture


class ItineraryKind(StrEnum):
    DOMESTIC = "domestic"
    INTERNATIONAL = "international"


class ConsumerDisposition(StrEnum):
    """Disposition of all alternative travel/compensation offers."""

    NO_ALTERNATIVE_OFFERED = "no_alternative_offered"
    REJECTED_ALL = "rejected_all"
    NO_RESPONSE_AND_FLIGHT_DEPARTED = "no_response_and_flight_departed"
    ACCEPTED_TRAVEL = "accepted_travel"
    ACCEPTED_COMPENSATION = "accepted_compensation"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DisruptionFacts:
    as_of_date: date
    covered_flight: bool | None
    merchant_of_record: bool | None
    itinerary_kind: ItineraryKind | None
    consumer_disposition: ConsumerDisposition = ConsumerDisposition.UNKNOWN
    cancelled: bool | None = None
    flight_number_changed: bool | None = None
    rebooked_on_renumbered_flight: bool | None = None
    departure_earlier_minutes: int | None = None
    arrival_later_minutes: int | None = None
    origin_or_destination_changed: bool | None = None
    added_connection: bool | None = None
    downgraded_class: bool | None = None
    passenger_has_disability: bool | None = None
    disability_connection_changed: bool | None = None
    needed_accessibility_feature_unavailable: bool | None = None


@dataclass(frozen=True)
class AirTravelDecision:
    entitlement: EntitlementDecision
    enforcement: EnforcementAssessment


def _significant_change_reasons(facts: DisruptionFacts) -> tuple[str, ...]:
    reasons: list[str] = []
    threshold = None
    if facts.itinerary_kind is ItineraryKind.DOMESTIC:
        threshold = 180
    elif facts.itinerary_kind is ItineraryKind.INTERNATIONAL:
        threshold = 360

    if threshold is not None:
        if (
            facts.departure_earlier_minutes is not None
            and facts.departure_earlier_minutes >= threshold
        ):
            reasons.append(f"departure moved earlier by at least {threshold} minutes")
        if (
            facts.arrival_later_minutes is not None
            and facts.arrival_later_minutes >= threshold
        ):
            reasons.append(f"arrival moved later by at least {threshold} minutes")

    if facts.origin_or_destination_changed:
        reasons.append("origin or destination airport changed")
    if facts.added_connection:
        reasons.append("replacement itinerary added a connection")
    if facts.downgraded_class:
        reasons.append("class of service was downgraded")
    if facts.passenger_has_disability and facts.disability_connection_changed:
        reasons.append("disabled passenger was routed through a different connection")
    if facts.passenger_has_disability and facts.needed_accessibility_feature_unavailable:
        reasons.append("needed accessibility feature is unavailable on substitute aircraft")
    return tuple(reasons)


def _missing_negative_predicate_facts(facts: DisruptionFacts) -> tuple[str, ...]:
    """Facts required before concluding no positive refund predicate applies."""

    missing: list[str] = []
    if facts.cancelled is None:
        missing.append("cancelled")
    if facts.flight_number_changed is None:
        missing.append("flight_number_changed")
    if facts.itinerary_kind is None:
        missing.append("itinerary_kind")
    if facts.arrival_later_minutes is None:
        missing.append("arrival_later_minutes")
    if facts.departure_earlier_minutes is None:
        missing.append("departure_earlier_minutes")
    for name in (
        "origin_or_destination_changed",
        "added_connection",
        "downgraded_class",
    ):
        if getattr(facts, name) is None:
            missing.append(name)

    for name in (
        "disability_connection_changed",
        "needed_accessibility_feature_unavailable",
    ):
        if (
            facts.passenger_has_disability is not False
            and getattr(facts, name) is not False
        ):
            if facts.passenger_has_disability is None:
                missing.append("passenger_has_disability")
            if getattr(facts, name) is None:
                missing.append(name)
    return tuple(missing)


def resolve_refund_entitlement(facts: DisruptionFacts) -> EntitlementDecision:
    """Resolve fare-refund eligibility under 14 CFR 260.2 and 260.6(a)."""

    missing_scope: list[str] = []
    if facts.covered_flight is None:
        missing_scope.append("covered_flight")
    if facts.merchant_of_record is None:
        missing_scope.append("merchant_of_record")
    if missing_scope:
        return EntitlementDecision(
            status=EntitlementStatus.UNRESOLVED,
            governing_clause_ids=("14CFR260.3", "14CFR260.6(a)(1)"),
            rationale_facts=(),
            unresolved_facts=tuple(missing_scope),
        )
    if not facts.covered_flight:
        return EntitlementDecision(
            status=EntitlementStatus.NOT_ELIGIBLE,
            governing_clause_ids=("14CFR260.3",),
            rationale_facts=("the itinerary is not a covered flight under Part 260",),
        )
    if not facts.merchant_of_record:
        return EntitlementDecision(
            status=EntitlementStatus.UNRESOLVED,
            governing_clause_ids=("14CFR260.3", "14CFR399.80(l)"),
            rationale_facts=("a ticket agent, rather than the carrier, is merchant of record",),
            unresolved_facts=("ticket_agent_refund_obligation",),
        )

    cancellation_basis = facts.cancelled is True or facts.flight_number_changed is True
    change_reasons = _significant_change_reasons(facts)
    disruption_reasons: list[str] = []
    if facts.cancelled is True:
        disruption_reasons.append("flight was cancelled")
    if facts.flight_number_changed is True:
        disruption_reasons.append("original numbered flight was not operated as sold")
    disruption_reasons.extend(change_reasons)

    if not cancellation_basis and not change_reasons:
        missing = _missing_negative_predicate_facts(facts)
        if missing:
            return EntitlementDecision(
                status=EntitlementStatus.UNRESOLVED,
                governing_clause_ids=("14CFR260.2",),
                rationale_facts=(),
                unresolved_facts=missing,
            )
        return EntitlementDecision(
            status=EntitlementStatus.NOT_ELIGIBLE,
            governing_clause_ids=("14CFR260.2",),
            rationale_facts=("no cancellation or defined significant change",),
        )

    if facts.consumer_disposition is ConsumerDisposition.UNKNOWN:
        return EntitlementDecision(
            status=EntitlementStatus.UNRESOLVED,
            governing_clause_ids=("14CFR260.6(a)(1)", "14CFR260.6(a)(2)"),
            rationale_facts=tuple(disruption_reasons),
            unresolved_facts=("consumer_disposition",),
        )

    if facts.consumer_disposition in {
        ConsumerDisposition.ACCEPTED_TRAVEL,
        ConsumerDisposition.ACCEPTED_COMPENSATION,
    }:
        return EntitlementDecision(
            status=EntitlementStatus.NOT_ELIGIBLE,
            governing_clause_ids=("14CFR260.6(a)(1)",),
            rationale_facts=tuple(disruption_reasons)
            + ("consumer accepted an alternative to a refund",),
        )

    return EntitlementDecision(
        status=EntitlementStatus.ELIGIBLE,
        governing_clause_ids=("14CFR260.2", "14CFR260.6(a)(1)", "14CFR260.6(a)(2)"),
        rationale_facts=tuple(disruption_reasons)
        + ("consumer did not accept alternative travel or compensation",),
    )


def resolve_air_travel(
    facts: DisruptionFacts,
    posture_registry: PostureRegistry | None = None,
) -> AirTravelDecision:
    """Resolve entitlement and enforcement as two explicit result fields."""

    registry = posture_registry or load_default_posture()
    entitlement = resolve_refund_entitlement(facts)
    significant_change = bool(_significant_change_reasons(facts))
    enforcement = registry.assess(
        as_of_date=facts.as_of_date,
        flight_renumbered=facts.flight_number_changed is True,
        rebooked_on_renumbered_flight=facts.rebooked_on_renumbered_flight,
        significant_change=significant_change,
    )
    return AirTravelDecision(entitlement=entitlement, enforcement=enforcement)
