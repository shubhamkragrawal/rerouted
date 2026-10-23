import json
import unittest
from datetime import date
from pathlib import Path

from core.resolver import EnforcementStatus
from domains.air_us.ingest.federal_register import (
    FederalRegisterIngestError,
    load_reviewed_notices,
    load_reviewed_posture_registry,
    parse_reviewed_notice,
)

FIXTURES = Path(__file__).parent / "fixtures" / "federal_register"


class FederalRegisterIngestTests(unittest.TestCase):
    def test_loads_reviewed_half_open_intervals_without_filling_gap(self) -> None:
        notices = load_reviewed_notices(FIXTURES / "reviewed_intervals.json")

        self.assertEqual(
            [notice.notice_id for notice in notices],
            ["FR-2025-22140", "FR-2026-13675"],
        )
        self.assertEqual(
            (notices[0].effective_from, notices[0].effective_to_exclusive),
            (date(2025, 12, 5), date(2026, 7, 1)),
        )
        self.assertEqual(
            (notices[1].effective_from, notices[1].effective_to_exclusive),
            (date(2026, 7, 7), date(2027, 7, 8)),
        )
        self.assertFalse(any(notice.in_force(date(2026, 7, 3)) for notice in notices))
        self.assertTrue(notices[1].in_force(date(2026, 7, 7)))
        self.assertFalse(notices[1].in_force(date(2027, 7, 8)))

    def test_changed_date_language_fails_closed(self) -> None:
        manifest = json.loads(
            (FIXTURES / "reviewed_intervals.json").read_text(encoding="utf-8")
        )
        review = manifest["notices"][0]
        payload = json.loads(
            (FIXTURES / review["payload_file"]).read_text(encoding="utf-8")
        )
        changed_text = "DATES: This notice applies as of December 5, 2025."

        with self.assertRaisesRegex(FederalRegisterIngestError, "missing reviewed anchor"):
            parse_reviewed_notice(payload, changed_text, review)

    def test_source_identity_must_match_review(self) -> None:
        manifest = json.loads(
            (FIXTURES / "reviewed_intervals.json").read_text(encoding="utf-8")
        )
        review = manifest["notices"][1]
        payload = json.loads(
            (FIXTURES / review["payload_file"]).read_text(encoding="utf-8")
        )
        text = (FIXTURES / review["text_file"]).read_text(encoding="utf-8")
        payload["document_number"] = "wrong-document"

        with self.assertRaisesRegex(FederalRegisterIngestError, "document_number"):
            parse_reviewed_notice(payload, text, review)

    def test_registry_preserves_verified_gap_provenance(self) -> None:
        registry = load_reviewed_posture_registry(
            FIXTURES / "reviewed_intervals.json"
        )

        assessment = registry.assess(
            as_of_date=date(2026, 7, 3),
            flight_renumbered=True,
            rebooked_on_renumbered_flight=True,
            significant_change=False,
        )

        self.assertEqual(
            assessment.status, EnforcementStatus.NO_PUBLISHED_CARVEOUT
        )
        self.assertEqual(
            assessment.regime_id,
            "no_published_renumbering_carveout_july_gap",
        )
        self.assertTrue(assessment.checked)
        self.assertIsNotNone(assessment.verification_note)


if __name__ == "__main__":
    unittest.main()
