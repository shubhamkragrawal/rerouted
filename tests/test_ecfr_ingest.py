import unittest
from datetime import date
from pathlib import Path

from domains.air_us.ingest.ecfr import (
    EcfrSnapshot,
    build_effective_intervals,
    parse_ecfr_xml,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ecfr"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class EcfrIngestionTests(unittest.TestCase):
    def test_parses_one_record_per_leaf_with_reconstructable_hierarchy(self) -> None:
        clauses = parse_ecfr_xml(
            fixture("part-260-2025-11-01.xml"),
            version_effective_on=date(2024, 8, 12),
            queried_on=date(2025, 11, 1),
            source_url="https://example.test/synthetic/ecfr/part-260.xml",
            document_metadata={"retrieved_by": "unit-test"},
        )

        by_id = {clause.clause_id: clause for clause in clauses}
        self.assertEqual(
            set(by_id),
            {
                "14CFR260.1",
                "14CFR260.6(a)",
                "14CFR260.6(a)(1)",
                "14CFR260.6(a)(2)",
                "14CFR260.6(a)(3)",
                "14CFR260.6(b)",
            },
        )
        leaf = by_id["14CFR260.6(a)(2)"]
        self.assertEqual(
            leaf.hierarchy,
            ("Title 14", "Part 260", "§ 260.6", "(a)", "(2)"),
        )
        self.assertEqual(
            leaf.ancestor_text,
            ("(a) A carrier must provide a prompt automatic refund when:",),
        )
        self.assertEqual(
            leaf.text,
            "(2) The passenger does not accept alternative transportation, travel "
            "credit, voucher, or other compensation; and",
        )
        self.assertEqual(
            leaf.text_hash,
            "850fa40b1487146e4f2583dcfcfc8197d68b6119697005ce96323811d326dce4",
        )
        self.assertIn("§ 260.6", leaf.heading)
        self.assertEqual(leaf.document_metadata["retrieved_by"], "unit-test")
        self.assertEqual(
            leaf.document_metadata["snapshot_sha256"],
            "665755940cf17aaaa7bbc0d94ecd3f0f9c3a087eba2629016c4077cbffbe54c7",
        )
        self.assertEqual(leaf.version_effective_on, date(2024, 8, 12))
        self.assertEqual(leaf.queried_on, date(2025, 11, 1))
        self.assertEqual(leaf.document_metadata["version_effective_on"], "2024-08-12")
        self.assertEqual(leaf.document_metadata["queried_on"], "2025-11-01")
        self.assertEqual(
            leaf.document_metadata["authority"],
            "49 U.S.C. 40101, 40113, 41712, and 42301.",
        )

    def test_identical_part_260_snapshots_make_one_open_interval_per_clause(self) -> None:
        snapshots = [
            EcfrSnapshot(
                version_effective_on=date(2024, 8, 12),
                queried_on=date(2025, 11, 1),
                xml_text=fixture("part-260-2025-11-01.xml"),
                source_url="https://example.test/synthetic/ecfr/2025-11-01.xml",
            ),
            EcfrSnapshot(
                version_effective_on=date(2024, 8, 12),
                queried_on=date(2026, 3, 14),
                xml_text=fixture("part-260-2026-03-14.xml"),
                source_url="https://example.test/synthetic/ecfr/2026-03-14.xml",
            ),
        ]

        intervals = build_effective_intervals(snapshots)

        self.assertEqual(len(intervals), 6)
        self.assertTrue(all(item.effective_from == date(2024, 8, 12) for item in intervals))
        self.assertTrue(all(item.effective_to is None for item in intervals))
        self.assertTrue(all(item.active_on(date(2025, 6, 1)) for item in intervals))
        self.assertTrue(all(item.active_on(date(2026, 3, 14)) for item in intervals))
        self.assertTrue(all(item.source_queried_on == date(2025, 11, 1) for item in intervals))
        # Repeated lookup dates never become fabricated effective dates.
        self.assertTrue(all("2025-11-01" in item.source_url for item in intervals))
        self.assertFalse(any(item.effective_from == date(2025, 11, 1) for item in intervals))
        self.assertEqual(
            {item.text_hash for item in intervals},
            {
                "31520cb433e60705b0deda346194518d71b31fb6ea32b0d0c6b26f8ab4defc4e",
                "92ecadf8181b91d9081460c30d646b20c5dcbab23bb0e603dbcf46b03ecf4c68",
                "850fa40b1487146e4f2583dcfcfc8197d68b6119697005ce96323811d326dce4",
                "017c567116daac788dc3ec68594351dd3134b51a2faab9ef1a3cba861176b88e",
                "36ce02eba669872fedfb8195e4e3f45dd2de4e899fe33e12b44fab2449260a77",
                "b2122381667dddea85a31a6cd472913da3a501b924e4545b06f4687ae2c02cf5",
            },
        )

    def test_changed_clause_closes_old_interval_with_half_open_semantics(self) -> None:
        original = fixture("part-260-2025-11-01.xml")
        changed = original.replace(
            "Refunds must be provided in the original form of payment.",
            "Refunds must be provided promptly in the original form of payment.",
        )
        intervals = build_effective_intervals(
            [
                EcfrSnapshot(
                    version_effective_on=date(2024, 8, 12),
                    queried_on=date(2025, 11, 1),
                    xml_text=original,
                    source_url="https://example.test/old.xml",
                ),
                EcfrSnapshot(
                    version_effective_on=date(2026, 3, 14),
                    queried_on=date(2026, 3, 14),
                    xml_text=changed,
                    source_url="https://example.test/new.xml",
                ),
            ]
        )
        versions = [item for item in intervals if item.clause_id == "14CFR260.6(b)"]

        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0].effective_to, date(2026, 3, 14))
        self.assertTrue(versions[0].active_on(date(2026, 3, 13)))
        self.assertFalse(versions[0].active_on(date(2026, 3, 14)))
        self.assertTrue(versions[1].active_on(date(2026, 3, 14)))
        self.assertNotEqual(versions[0].text_hash, versions[1].text_hash)

        indexed_old = versions[0].to_indexed_document()
        indexed_new = versions[1].to_indexed_document()
        self.assertEqual(indexed_old.effective_to, date(2026, 3, 13))
        self.assertTrue(indexed_old.active_on(date(2026, 3, 13)))
        self.assertFalse(indexed_old.active_on(date(2026, 3, 14)))
        self.assertTrue(indexed_new.active_on(date(2026, 3, 14)))
        self.assertEqual(
            indexed_old.metadata["effective_to_exclusive"], "2026-03-14"
        )

    def test_clause_ids_use_xml_labels_not_paragraph_ordinals(self) -> None:
        xml = fixture("part-260-2025-11-01.xml").replace("<P>(3)", "<P>(7)")

        clauses = parse_ecfr_xml(
            xml,
            version_effective_on=date(2024, 8, 12),
            queried_on=date(2025, 11, 1),
            source_url="https://example.test/non-sequential.xml",
        )

        ids = {clause.clause_id for clause in clauses}
        self.assertIn("14CFR260.6(a)(7)", ids)
        self.assertNotIn("14CFR260.6(a)(3)", ids)

    def test_groups_multiple_unnumbered_definitions_under_official_citation(self) -> None:
        clauses = parse_ecfr_xml(
            fixture("part-260-definitions-synthetic.xml"),
            version_effective_on=date(2024, 8, 12),
            queried_on=date(2026, 3, 14),
            source_url="https://example.test/synthetic/ecfr/definitions.xml",
        )

        self.assertEqual(len(clauses), 1)
        definitions = clauses[0]
        self.assertEqual(definitions.document_id, "14CFR260.2")
        self.assertEqual(definitions.clause_id, "14CFR260.2")
        self.assertEqual(definitions.paragraph_path, ())
        self.assertEqual(definitions.text.count("\n\n"), 2)
        self.assertIn("Cancelled flight means", definitions.text)
        self.assertIn("Carrier means", definitions.text)
        self.assertIn("Significant change means", definitions.text)

    def test_preserves_inline_nested_label_after_italic_heading(self) -> None:
        xml = """<ECFR><DIV5 TYPE="PART" N="260"><DIV8 TYPE="SECTION">
        <HEAD>§ 260.6 Refunds.</HEAD><P>(a) <I>Carrier obligation.</I>—(1)
        <I>General.</I> A carrier must provide a refund.</P>
        <P>(i) The flight was cancelled.</P><P>(ii) The passenger declined travel.</P>
        <P>(2) The refund is automatic.</P></DIV8></DIV5></ECFR>"""

        clauses = parse_ecfr_xml(
            xml,
            version_effective_on=date(2024, 8, 12),
            queried_on=date(2026, 10, 8),
            source_url="https://www.ecfr.gov/api/versioner/v1/full/2024-08-12/title-14.xml",
        )

        self.assertEqual(
            {clause.clause_id for clause in clauses},
            {
                "14CFR260.6(a)(1)",
                "14CFR260.6(a)(1)(i)",
                "14CFR260.6(a)(1)(ii)",
                "14CFR260.6(a)(2)",
            },
        )
        by_id = {clause.clause_id: clause for clause in clauses}
        self.assertEqual(
            by_id["14CFR260.6(a)(1)(i)"].ancestor_text,
            ("(a) Carrier obligation.—(1) General. A carrier must provide a refund.",),
        )

    def test_rejects_conflicting_observations_of_one_effective_version(self) -> None:
        xml = fixture("part-260-2025-11-01.xml")
        changed = xml.replace(
            "Refunds must be provided in the original form of payment.",
            "Refunds must be provided promptly in the original form of payment.",
        )
        snapshots = [
            EcfrSnapshot(
                date(2024, 8, 12),
                date(2025, 11, 1),
                xml,
                "https://example.test/a.xml",
            ),
            EcfrSnapshot(
                date(2024, 8, 12),
                date(2026, 3, 14),
                changed,
                "https://example.test/b.xml",
            ),
        ]

        with self.assertRaisesRegex(ValueError, "conflicting snapshots"):
            build_effective_intervals(snapshots)


if __name__ == "__main__":
    unittest.main()
