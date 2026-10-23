import unittest
from dataclasses import replace
from datetime import date

from core.resolver import (
    Authority,
    AuthorityConflict,
    EnforcementStatus,
    EntitlementStatus,
    RuleCandidate,
    choose_authoritative_rule,
)
from domains.air_us.rules import (
    ConsumerDisposition,
    DisruptionFacts,
    ItineraryKind,
    resolve_air_travel,
    resolve_refund_entitlement,
)


def complete_facts(**overrides: object) -> DisruptionFacts:
    base = DisruptionFacts(
        as_of_date=date(2026, 3, 14),
        covered_flight=True,
        merchant_of_record=True,
        itinerary_kind=ItineraryKind.DOMESTIC,
        consumer_disposition=ConsumerDisposition.REJECTED_ALL,
        cancelled=False,
        flight_number_changed=False,
        rebooked_on_renumbered_flight=False,
        departure_earlier_minutes=0,
        arrival_later_minutes=0,
        origin_or_destination_changed=False,
        added_connection=False,
        downgraded_class=False,
        passenger_has_disability=False,
        disability_connection_changed=False,
        needed_accessibility_feature_unavailable=False,
    )
    return replace(base, **overrides)


class RefundResolverTests(unittest.TestCase):
    def test_domestic_three_hour_boundary_is_inclusive(self) -> None:
        at_boundary = resolve_refund_entitlement(
            complete_facts(arrival_later_minutes=180)
        )
        below_boundary = resolve_refund_entitlement(
            complete_facts(arrival_later_minutes=179)
        )
        self.assertEqual(at_boundary.status, EntitlementStatus.ELIGIBLE)
        self.assertEqual(below_boundary.status, EntitlementStatus.NOT_ELIGIBLE)

    def test_international_six_hour_boundary_is_inclusive(self) -> None:
        decision = resolve_refund_entitlement(
            complete_facts(
                itinerary_kind=ItineraryKind.INTERNATIONAL,
                arrival_later_minutes=360,
            )
        )
        self.assertEqual(decision.status, EntitlementStatus.ELIGIBLE)

    def test_non_time_significant_changes_qualify(self) -> None:
        for field in ("origin_or_destination_changed", "added_connection", "downgraded_class"):
            with self.subTest(field=field):
                decision = resolve_refund_entitlement(complete_facts(**{field: True}))
                self.assertEqual(decision.status, EntitlementStatus.ELIGIBLE)

    def test_accepting_rebooking_means_refund_predicate_is_not_met(self) -> None:
        decision = resolve_refund_entitlement(
            complete_facts(
                cancelled=True,
                consumer_disposition=ConsumerDisposition.ACCEPTED_TRAVEL,
            )
        )
        self.assertEqual(decision.status, EntitlementStatus.NOT_ELIGIBLE)

    def test_missing_load_bearing_fact_refuses_to_guess(self) -> None:
        decision = resolve_refund_entitlement(
            complete_facts(
                cancelled=True,
                consumer_disposition=ConsumerDisposition.UNKNOWN,
            )
        )
        self.assertEqual(decision.status, EntitlementStatus.UNRESOLVED)
        self.assertEqual(decision.unresolved_facts, ("consumer_disposition",))

    def test_negative_decision_requires_all_cancellation_and_change_predicates_known(self) -> None:
        for field in ("cancelled", "flight_number_changed"):
            with self.subTest(field=field):
                decision = resolve_refund_entitlement(complete_facts(**{field: None}))
                self.assertEqual(decision.status, EntitlementStatus.UNRESOLVED)
                self.assertIn(field, decision.unresolved_facts)

    def test_negative_decision_requires_disability_predicates_to_be_ruled_out(self) -> None:
        decision = resolve_refund_entitlement(
            complete_facts(
                passenger_has_disability=None,
                disability_connection_changed=None,
                needed_accessibility_feature_unavailable=None,
            )
        )
        self.assertEqual(decision.status, EntitlementStatus.UNRESOLVED)
        self.assertIn("passenger_has_disability", decision.unresolved_facts)
        self.assertIn("disability_connection_changed", decision.unresolved_facts)
        self.assertIn("needed_accessibility_feature_unavailable", decision.unresolved_facts)

    def test_known_no_disability_rules_out_missing_accessibility_details(self) -> None:
        decision = resolve_refund_entitlement(
            complete_facts(
                passenger_has_disability=False,
                disability_connection_changed=None,
                needed_accessibility_feature_unavailable=None,
            )
        )
        self.assertEqual(decision.status, EntitlementStatus.NOT_ELIGIBLE)

    def test_ticket_agent_merchant_routes_to_unresolved_ticket_agent_rule(self) -> None:
        decision = resolve_refund_entitlement(
            complete_facts(cancelled=True, merchant_of_record=False)
        )
        self.assertEqual(decision.status, EntitlementStatus.UNRESOLVED)
        self.assertEqual(decision.unresolved_facts, ("ticket_agent_refund_obligation",))

    def test_entitlement_survives_enforcement_discretion(self) -> None:
        decision = resolve_air_travel(
            complete_facts(
                flight_number_changed=True,
                rebooked_on_renumbered_flight=True,
                as_of_date=date(2026, 3, 14),
            )
        )
        self.assertEqual(decision.entitlement.status, EntitlementStatus.ELIGIBLE)
        self.assertEqual(decision.enforcement.status, EnforcementStatus.DISCRETION)


class AuthorityPrecedenceTests(unittest.TestCase):
    def test_regulation_wins_direct_conflict(self) -> None:
        selected = choose_authoritative_rule(
            [
                RuleCandidate("carrier-contract", Authority.CONTRACT, "deny", "contract"),
                RuleCandidate("14CFR260.6", Authority.REGULATION, "eligible", "regulation"),
            ]
        )
        self.assertEqual(selected.rule_id, "14CFR260.6")

    def test_enforcement_policy_cannot_decide_entitlement(self) -> None:
        with self.assertRaises(AuthorityConflict):
            choose_authoritative_rule(
                [
                    RuleCandidate(
                        "FR-2025-22140",
                        Authority.ENFORCEMENT_POLICY,
                        "not_eligible",
                        "incorrect conflation",
                    )
                ]
            )


if __name__ == "__main__":
    unittest.main()
