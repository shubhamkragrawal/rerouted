import unittest
from datetime import date

from core.resolver import EnforcementStatus
from domains.air_us.rules import load_default_posture


class PostureRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_default_posture()

    def test_four_dates_produce_four_distinct_regimes(self) -> None:
        dates = (
            date(2025, 6, 1),
            date(2026, 3, 14),
            date(2026, 7, 3),
            date(2026, 8, 2),
        )
        answers = [
            self.registry.assess(
                as_of_date=value,
                flight_renumbered=True,
                rebooked_on_renumbered_flight=True,
                significant_change=False,
            )
            for value in dates
        ]

        self.assertEqual(len({answer.regime_id for answer in answers}), 4)
        self.assertEqual(
            answers[0].status,
            EnforcementStatus.NO_PUBLISHED_CARVEOUT,
        )
        self.assertEqual(answers[1].status, EnforcementStatus.DISCRETION)
        self.assertEqual(
            answers[2].status,
            EnforcementStatus.NO_PUBLISHED_CARVEOUT,
        )
        self.assertEqual(answers[3].status, EnforcementStatus.DISCRETION)

    def test_empty_gap_is_checked_data_not_not_checked(self) -> None:
        lookup = self.registry.lookup(date(2026, 7, 3))
        assessment = self.registry.assess(
            as_of_date=date(2026, 7, 3),
            flight_renumbered=True,
            rebooked_on_renumbered_flight=True,
            significant_change=False,
        )

        self.assertTrue(lookup.checked)
        self.assertEqual(lookup.applicable_notices, ())
        self.assertTrue(assessment.checked)
        self.assertFalse(assessment.provisional)
        self.assertIsNotNone(assessment.verification_note)

    def test_notice_does_not_pause_out_of_scope_significant_change(self) -> None:
        assessment = self.registry.assess(
            as_of_date=date(2026, 3, 14),
            flight_renumbered=True,
            rebooked_on_renumbered_flight=True,
            significant_change=True,
        )
        self.assertEqual(assessment.status, EnforcementStatus.ACTIVE)
        self.assertEqual(assessment.applicable_notice_ids, ("FR-2025-22140",))

    def test_half_open_boundaries_and_refresh_horizon(self) -> None:
        fixtures = {
            date(2025, 12, 4): EnforcementStatus.NO_PUBLISHED_CARVEOUT,
            date(2025, 12, 5): EnforcementStatus.DISCRETION,
            date(2026, 6, 30): EnforcementStatus.DISCRETION,
            date(2026, 7, 1): EnforcementStatus.NO_PUBLISHED_CARVEOUT,
            date(2026, 7, 6): EnforcementStatus.NO_PUBLISHED_CARVEOUT,
            date(2026, 7, 7): EnforcementStatus.DISCRETION,
            date(2027, 7, 7): EnforcementStatus.DISCRETION,
            date(2027, 7, 8): EnforcementStatus.NOT_CHECKED,
        }
        for value, expected in fixtures.items():
            with self.subTest(as_of_date=value):
                actual = self.registry.assess(
                    as_of_date=value,
                    flight_renumbered=True,
                    rebooked_on_renumbered_flight=True,
                    significant_change=False,
                )
                self.assertEqual(actual.status, expected)

    def test_missing_rebooking_fact_makes_notice_scope_unresolved(self) -> None:
        assessment = self.registry.assess(
            as_of_date=date(2026, 3, 14),
            flight_renumbered=True,
            rebooked_on_renumbered_flight=None,
            significant_change=False,
        )
        self.assertEqual(assessment.status, EnforcementStatus.UNRESOLVED)
        self.assertEqual(
            assessment.unresolved_facts,
            ("rebooked_on_renumbered_flight",),
        )


if __name__ == "__main__":
    unittest.main()
