"""Composed local test for both layers of the rules retrieval pipeline."""

import unittest
from datetime import date
from pathlib import Path

from core.contracts import Authority, SearchRulesRequest
from domains.air_us.ingest import (
    EcfrSnapshot,
    build_effective_intervals,
    load_reviewed_posture_registry,
)
from domains.air_us.tools import SearchRulesService

FIXTURES = Path(__file__).parent / "fixtures"
ECFR_FIXTURES = FIXTURES / "ecfr"
FR_MANIFEST = FIXTURES / "federal_register" / "reviewed_intervals.json"


def _ecfr_fixture(name: str) -> str:
    return (ECFR_FIXTURES / name).read_text(encoding="utf-8")


def _service() -> SearchRulesService:
    original = _ecfr_fixture("part-260-2025-11-01.xml")
    unchanged = _ecfr_fixture("part-260-2026-03-14.xml")
    future_change = unchanged.replace(
        "Refunds must be provided in the original form of payment.",
        "Refunds must be provided promptly in the original form of payment.",
    )
    intervals = build_effective_intervals(
        (
            EcfrSnapshot(
                version_effective_on=date(2024, 8, 12),
                queried_on=date(2025, 11, 1),
                xml_text=original,
                source_url="https://example.test/synthetic/ecfr/2025-11-01.xml",
            ),
            EcfrSnapshot(
                version_effective_on=date(2024, 8, 12),
                queried_on=date(2026, 3, 14),
                xml_text=unchanged,
                source_url="https://example.test/synthetic/ecfr/2026-03-14.xml",
            ),
            EcfrSnapshot(
                version_effective_on=date(2026, 8, 1),
                queried_on=date(2026, 8, 1),
                xml_text=future_change,
                source_url="https://example.test/synthetic/ecfr/2026-08-01.xml",
            ),
        )
    )
    return SearchRulesService(
        (interval.to_indexed_document() for interval in intervals),
        posture_registry=load_reviewed_posture_registry(FR_MANIFEST),
        corpus_version="rules-composed-fixture-v1",
    )


def _search(service: SearchRulesService, as_of_date: date):
    return service.search(
        SearchRulesRequest(
            query="refund carrier passenger alternative transportation",
            as_of_date=as_of_date,
            doc_types=(Authority.REGULATION, Authority.ENFORCEMENT_POLICY),
            k=100,
        )
    )


class RulesPipelineTests(unittest.TestCase):
    def test_composed_temporal_rules_and_posture_pipeline(self) -> None:
        service = _service()
        november = _search(service, date(2025, 11, 1))
        march = _search(service, date(2026, 3, 14))
        gap = _search(service, date(2026, 7, 3))
        successor = _search(service, date(2026, 7, 7))

        november_clauses = {
            clause.clause_id: clause.text for clause in november.clauses
        }
        march_clauses = {clause.clause_id: clause.text for clause in march.clauses}
        self.assertEqual(november_clauses, march_clauses)
        self.assertEqual(len(november_clauses), 6)
        self.assertIn("14CFR260.6(a)", november_clauses)

        self.assertTrue(gap.posture_checked)
        self.assertEqual(gap.posture, ())
        self.assertTrue(successor.posture_checked)
        self.assertEqual(
            [item.fr_doc for item in successor.posture], ["FR-2026-13675"]
        )

        # The future clause version is visible as a date exclusion at each queried
        # date, proving temporal filtering is applied inside the composed pipeline.
        self.assertEqual(
            [
                november.excluded_by_date,
                march.excluded_by_date,
                gap.excluded_by_date,
                successor.excluded_by_date,
            ],
            [1, 1, 1, 1],
        )


if __name__ == "__main__":
    unittest.main()
