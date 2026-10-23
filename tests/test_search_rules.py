import unittest
from datetime import date
from pathlib import Path

from core.contracts import Authority, SearchRulesRequest
from core.retrieval import IndexedDocument
from domains.air_us.ingest import load_reviewed_posture_registry
from domains.air_us.tools.search_rules import SearchRulesService

SOURCE = "https://www.ecfr.gov/current/title-14/section-260.6"
FR_FIXTURES = Path(__file__).parent / "fixtures" / "federal_register"


def _service() -> SearchRulesService:
    posture_registry = load_reviewed_posture_registry(
        FR_FIXTURES / "reviewed_intervals.json"
    )
    return SearchRulesService(
        [
            IndexedDocument(
                document_id="expired-exact-match",
                text="260.6 refund significant change refund significant change",
                authority="regulation",
                doc_type="regulation",
                effective_from=date(2024, 1, 1),
                effective_to=date(2025, 12, 31),
                source_url=SOURCE,
                metadata={"doc": "14 CFR Part 260 (superseded fixture)"},
            ),
            IndexedDocument(
                document_id="14CFR260.6(a)",
                text="A carrier shall provide a prompt refund after a cancellation.",
                authority="regulation",
                doc_type="regulation",
                effective_from=date(2026, 1, 1),
                source_url=SOURCE,
                metadata={"doc": "14 CFR Part 260"},
            ),
            IndexedDocument(
                document_id="AA-contract-meals",
                text="Meal voucher after a controllable delay.",
                authority="contract",
                doc_type="contract",
                carrier="AA",
                effective_from=date(2026, 1, 1),
                source_url=(
                    "https://www.aa.com/i18n/customer-service/support/"
                    "conditions-of-carriage.jsp"
                ),
                metadata={"doc": "American Airlines conditions of carriage"},
            ),
            IndexedDocument(
                document_id="must-never-be-a-clause",
                text="renumbered flight enforcement discretion",
                authority="enforcement_policy",
                doc_type="enforcement_policy",
                effective_from=date(2026, 1, 1),
                source_url=(
                    "https://www.federalregister.gov/documents/2026/07/07/"
                    "2026-13675/airline-refunds-and-other-consumer-protections"
                ),
            ),
        ],
        posture_registry=posture_registry,
        corpus_version="rules-test-v1",
    )


def _request(as_of_date: date, *, k: int = 1) -> SearchRulesRequest:
    return SearchRulesRequest(
        query="260.6 refund significant change",
        as_of_date=as_of_date,
        doc_types=(Authority.REGULATION, Authority.ENFORCEMENT_POLICY),
        k=k,
    )


class SearchRulesTests(unittest.TestCase):
    def test_expired_exact_match_cannot_take_k_one_slot(self) -> None:
        result = _service().search(_request(date(2026, 7, 3), k=1))

        self.assertEqual(
            [clause.clause_id for clause in result.clauses], ["14CFR260.6(a)"]
        )
        # Only the expired regulation passes authority/carrier predicates and then
        # fails the date predicate. The AA contract is excluded by authority, not date.
        self.assertEqual(result.excluded_by_date, 1)

    def test_adds_served_retrieval_diagnostics(self) -> None:
        result = _service().search(_request(date(2026, 7, 3)))
        self.assertEqual(result.retrieval_provider, "hash")
        self.assertEqual(result.retrieval_model, "blake2b-token-hash-v1")
        self.assertIsNone(result.retrieval_fallback_reason)

    def test_july_gap_is_empty_but_checked_posture(self) -> None:
        result = _service().search(_request(date(2026, 7, 3)))

        self.assertTrue(result.posture_checked)
        self.assertEqual(result.posture, ())

    def test_july_seventh_returns_exact_successor_notice_and_boundary(self) -> None:
        result = _service().search(_request(date(2026, 7, 7)))

        self.assertTrue(result.posture_checked)
        self.assertEqual(len(result.posture), 1)
        self.assertEqual(result.posture[0].fr_doc, "FR-2026-13675")
        self.assertEqual(result.posture[0].effective_from, date(2026, 7, 7))
        self.assertEqual(result.posture[0].effective_to, date(2027, 7, 7))

    def test_posture_after_review_horizon_is_not_checked(self) -> None:
        result = _service().search(_request(date(2027, 7, 8)))

        self.assertFalse(result.posture_checked)
        self.assertEqual(result.posture, ())

    def test_enforcement_only_request_never_returns_policy_as_clause(self) -> None:
        result = _service().search(
            SearchRulesRequest(
                query="renumbered flight",
                as_of_date=date(2026, 7, 7),
                doc_types=(Authority.ENFORCEMENT_POLICY,),
                k=10,
            )
        )

        self.assertEqual(result.clauses, ())
        self.assertEqual(
            [item.fr_doc for item in result.posture], ["FR-2026-13675"]
        )
        self.assertEqual(result.excluded_by_date, 0)

    def test_empty_doc_types_excludes_posture_documents_from_clauses(self) -> None:
        result = _service().search(
            SearchRulesRequest(
                query="renumbered flight enforcement discretion",
                as_of_date=date(2026, 7, 7),
                doc_types=(),
                k=10,
            )
        )

        self.assertNotIn(
            "must-never-be-a-clause",
            [clause.clause_id for clause in result.clauses],
        )
        self.assertEqual(
            [item.fr_doc for item in result.posture], ["FR-2026-13675"]
        )

    def test_exposes_canonical_clause_id_for_versioned_internal_document(self) -> None:
        service = SearchRulesService(
            [
                IndexedDocument(
                    document_id="14CFR260.6(a)(1)@2024-08-12",
                    text="A carrier must provide a prompt automatic refund.",
                    authority="regulation",
                    doc_type="regulation",
                    effective_from=date(2024, 8, 12),
                    source_url=SOURCE,
                    metadata={"clause_id": "14CFR260.6(a)(1)"},
                )
            ]
        )

        result = service.search(
            SearchRulesRequest(
                query="automatic refund",
                as_of_date=date(2026, 1, 1),
                doc_types=(Authority.REGULATION,),
                k=1,
            )
        )

        self.assertEqual(result.clauses[0].clause_id, "14CFR260.6(a)(1)")


if __name__ == "__main__":
    unittest.main()
