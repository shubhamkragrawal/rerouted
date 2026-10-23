import json
from datetime import date
from pathlib import Path

import pytest

from domains.air_us.ingest.source_corpus import (
    SourceCorpusError,
    ingest_manifest,
    normalize_source,
)

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "data/rules/source_manifest.json"


def test_reviewed_contract_and_dot_sources_produce_clause_records() -> None:
    records = ingest_manifest(MANIFEST)

    contracts = [record for record in records if record["authority"] == "contract"]
    commitments = [record for record in records if record["authority"] == "commitment"]
    assert {record["carrier"] for record in contracts} == {"AA", "DL", "UA"}
    assert len(contracts) == 7
    assert len(commitments) == 6
    for record in records:
        metadata = record["metadata"]
        assert len(metadata["source_sha256"]) == 64
        assert len(metadata["clause_sha256"]) == 64
        assert metadata["retrieved_on"] == "2026-10-08"
        assert record["source_url"].startswith("https://")
        assert date.fromisoformat(record["effective_from"])
    assert {record["source_url"] for record in contracts} == {
        "https://www.aa.com/web/i18n/customer-service/support/conditions-of-carriage.html",
        "https://www.delta.com/us/en/legal/contract-of-carriage-dgr",
        "https://www.united.com/en/us/fly/contract-of-carriage.html",
    }
    assert all(
        record["metadata"]["source_hash_kind"] == "sha256_of_local_snapshot_bytes"
        for record in records
    )


def test_changed_review_anchor_fails_closed_without_network(tmp_path: Path) -> None:
    changed = tmp_path / "changed.txt"
    changed.write_text("updated page with changed reviewed text", encoding="utf-8")

    with pytest.raises(SourceCorpusError, match="changed or lost reviewed anchor"):
        ingest_manifest(MANIFEST, input_overrides={"dot-dashboard-aa": changed})


def test_saved_html_and_text_are_normalized_locally(tmp_path: Path) -> None:
    html_path = tmp_path / "fixture.html"
    html_path.write_text(
        "<main><h1>Rule 24</h1><p>Delayed &amp; canceled</p></main>", encoding="utf-8"
    )

    assert "Rule 24" in normalize_source(html_path, html_path.read_bytes())
    assert "Delayed & canceled" in normalize_source(html_path, html_path.read_bytes())


def test_reviewed_source_snapshots_have_no_patch_or_manifest_contamination() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    source_files = sorted(
        {
            MANIFEST.parent / source["snapshot_file"]
            for source in manifest["sources"]
        }
    )
    assert len(source_files) == 4
    for path in source_files:
        text = path.read_text(encoding="utf-8")
        assert "*** Add File:" not in text
        assert "*** Begin Patch" not in text
        assert "*** End Patch" not in text
        assert '"schema_version"' not in text
        assert '"source_id":' not in text
        assert '"snapshot_file":' not in text


def test_manifest_contains_exactly_three_carrier_contract_records() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    contracts = [source for source in manifest["sources"] if source["authority"] == "contract"]
    assert {source["carrier"] for source in contracts} == {"AA", "DL", "UA"}
