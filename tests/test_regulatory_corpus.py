import json
from datetime import date
from pathlib import Path

import pytest

from domains.air_us.ingest import regulatory_corpus
from domains.air_us.ingest.regulatory_corpus import (
    RegulatoryCorpusError,
    _records_from_intervals,
    _review_manifest,
    _version_dates,
    fetch_and_validate_notices,
    merge_records,
)

ROOT = Path(__file__).parents[1]


def test_version_dates_are_exact_effective_dates_and_part_scoped() -> None:
    rows = (
        {"part": "260", "date": "2024-08-12", "amendment_date": "2024-08-12"},
        {"part": "260", "date": "2025-12-05", "amendment_date": "2025-12-05"},
        {"part": "259", "date": "2025-01-01", "amendment_date": "2025-01-01"},
    )

    assert _version_dates(
        rows, part=260, start_date=date(2024, 1, 1), query_date=date(2026, 1, 1)
    ) == (date(2024, 8, 12), date(2025, 12, 5))


def test_version_schema_drift_fails_closed() -> None:
    with pytest.raises(RegulatoryCorpusError, match="date and amendment_date"):
        _version_dates(
            ({"part": "260", "date": "2025-12-05", "amendment_date": "2025-12-06"},),
            part=260,
            start_date=date(2025, 1, 1),
            query_date=date(2026, 1, 1),
        )


def test_idempotent_merge_and_conflicting_document_ids() -> None:
    record = {"document_id": "14CFR260.6(a)", "authority": "regulation", "text": "Rule"}

    assert merge_records((record,), (record,)) == [record]
    with pytest.raises(RegulatoryCorpusError, match="collision"):
        merge_records((record,), ({**record, "text": "Changed"},))


def test_regulatory_records_keep_clause_versions_distinct() -> None:
    from domains.air_us.ingest.ecfr import EcfrSnapshot, build_effective_intervals

    fixture_path = ROOT / "tests/fixtures/ecfr/part-260-2025-11-01.xml"
    original = fixture_path.read_text(encoding="utf-8")
    revised = original.replace(
        "Refunds must be provided in the original form of payment.",
        "Refunds must be provided promptly in the original form of payment.",
    )
    intervals = build_effective_intervals(
        (
            EcfrSnapshot(
                date(2024, 8, 12),
                date(2026, 10, 8),
                original,
                "https://www.ecfr.gov/synthetic/old.xml",
            ),
            EcfrSnapshot(
                date(2026, 8, 1),
                date(2026, 10, 8),
                revised,
                "https://www.ecfr.gov/synthetic/new.xml",
            ),
        )
    )

    records = _records_from_intervals(intervals)
    ids = [record["document_id"] for record in records]
    assert len(ids) == len(set(ids))
    assert any(item.endswith("@2024-08-12") for item in ids)
    assert any(item.endswith("@2026-08-01") for item in ids)
    assert all(
        record["metadata"]["source_hash_kind"] == "sha256_of_ecfr_xml_snapshot_bytes"
        for record in records
    )


def test_production_posture_manifest_is_reviewed_source_gate() -> None:
    reviews = _review_manifest(ROOT / "domains/air_us/posture.yaml")

    assert [review["document_number"] for review in reviews] == [
        "2025-22140",
        "2026-13675",
    ]
    assert all(review["required_text"] for review in reviews)
    assert all(review["effective_to_exclusive"] for review in reviews)


def test_review_gate_accepts_only_local_synthetic_notice_fixtures(monkeypatch) -> None:
    fixtures = ROOT / "tests/fixtures/federal_register"
    payloads = {}
    texts = {}
    for number in ("2025-22140", "2026-13675"):
        payload = json.loads((fixtures / f"{number}.json").read_text(encoding="utf-8"))
        payload["raw_text_url"] = f"https://www.federalregister.gov/test/{number}.txt"
        payloads[number] = json.dumps(payload).encode("utf-8")
        texts[number] = (fixtures / f"{number}.txt").read_bytes()

    def fake_read_url(url: str, *, max_bytes: int = 40_000_000) -> bytes:
        for number in payloads:
            if url.endswith(f"/{number}.json"):
                return payloads[number]
            if url.endswith(f"/{number}.txt"):
                return texts[number]
        raise AssertionError(f"unexpected URL in fixture test: {url}")

    monkeypatch.setattr(regulatory_corpus, "_read_url", fake_read_url)
    validated = fetch_and_validate_notices(
        review_manifest=ROOT / "domains/air_us/posture.yaml",
    )

    assert [item[0]["document_number"] for item in validated] == [
        "2025-22140",
        "2026-13675",
    ]
    assert all("SYNTHETIC PARSER FIXTURE" in item[1] for item in validated)
