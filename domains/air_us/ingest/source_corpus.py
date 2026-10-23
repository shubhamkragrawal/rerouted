"""Review-gated ingestion for carrier contracts and DOT dashboard commitments.

The manifest stores the small set of clauses reviewed for this product, while source
snapshots preserve the exact bytes used for the review.  Ingestion verifies every
review anchor and every clause verbatim before producing retrieval-ready JSONL.
It intentionally does not infer legal effective dates from a retrieval date.
"""

from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any


class SourceCorpusError(ValueError):
    """Raised when source text does not match its reviewed manifest."""


class _TextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def normalize_source(path: Path, raw: bytes) -> str:
    """Extract searchable text from HTML, PDF, or UTF-8 text without rewriting claims."""

    suffix = path.suffix.casefold()
    if suffix in {".txt", ".text", ".md", ".json"}:
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourceCorpusError(f"source is not valid UTF-8 text: {path}") from exc
    if suffix in {".html", ".htm"}:
        try:
            html_text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourceCorpusError(f"source is not valid UTF-8 HTML: {path}") from exc
        extractor = _TextExtractor()
        extractor.feed(html_text)
        extractor.close()
        return "\n".join(part for part in extractor.parts if part.strip())
    if suffix == ".pdf":
        executable = shutil.which("pdftotext")
        if executable is None:
            raise SourceCorpusError("PDF input requires the pdftotext utility")
        result = subprocess.run(
            [executable, "-layout", str(path), "-"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise SourceCorpusError(f"could not extract PDF text: {path}")
        return result.stdout
    raise SourceCorpusError(f"unsupported source format: {path.suffix or '(no suffix)'}")


def _normalized(value: str) -> str:
    return " ".join(value.split()).casefold()


def _required_string(record: Mapping[str, Any], key: str, context: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SourceCorpusError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _date(value: Any, *, context: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise SourceCorpusError(f"{context} must be an ISO date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise SourceCorpusError(f"{context} must be an ISO date: {value!r}") from exc


def _verify_reviewed_text(
    source_text: str, source: Mapping[str, Any], source_id: str
) -> list[Mapping[str, Any]]:
    review_anchors = source.get("review_anchors")
    if not isinstance(review_anchors, list) or not review_anchors:
        raise SourceCorpusError(f"{source_id} needs at least one reviewed anchor")
    normalized_source = _normalized(source_text)
    for anchor in review_anchors:
        if not isinstance(anchor, str) or not anchor.strip():
            raise SourceCorpusError(f"{source_id} contains an invalid review anchor")
        if _normalized(anchor) not in normalized_source:
            raise SourceCorpusError(
                f"{source_id} changed or lost reviewed anchor: {anchor!r}"
            )
    clauses = source.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        raise SourceCorpusError(f"{source_id} needs reviewed clause records")
    for index, clause in enumerate(clauses):
        if not isinstance(clause, dict):
            raise SourceCorpusError(f"{source_id}.clauses[{index}] must be an object")
        clause_text = _required_string(clause, "text", f"{source_id}.clauses[{index}]")
        if _normalized(clause_text) not in normalized_source:
            raise SourceCorpusError(
                f"{source_id} clause text is not present in its source snapshot: "
                f"{clause.get('clause_id', index)}"
            )
    return clauses


def ingest_manifest(
    manifest_path: Path,
    *,
    input_overrides: Mapping[str, Path] | None = None,
) -> list[dict[str, Any]]:
    """Verify source snapshots and return retrieval-index records."""

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceCorpusError(f"could not read manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise SourceCorpusError("manifest must be an object with schema_version 1")
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise SourceCorpusError("manifest.sources must be a list")

    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    contract_carriers: set[str] = set()
    overrides = input_overrides or {}
    for source in sources:
        if not isinstance(source, dict):
            raise SourceCorpusError("each source must be an object")
        source_id = _required_string(source, "source_id", "source")
        if source_id in seen_ids:
            raise SourceCorpusError(f"duplicate source_id: {source_id}")
        seen_ids.add(source_id)
        source_path = overrides.get(source_id)
        if source_path is None:
            relative_path = _required_string(source, "snapshot_file", source_id)
            source_path = manifest_path.parent / relative_path
        try:
            raw = source_path.read_bytes()
        except OSError as exc:
            raise SourceCorpusError(f"could not read {source_id} snapshot: {source_path}") from exc
        source_text = normalize_source(source_path, raw)
        clauses = _verify_reviewed_text(source_text, source, source_id)
        retrieved_on = _date(source.get("retrieved_on"), context=f"{source_id}.retrieved_on")
        effective_from = _date(
            source.get("effective_from"),
            context=f"{source_id}.effective_from",
            nullable=True,
        )
        effective_to = _date(
            source.get("effective_to"),
            context=f"{source_id}.effective_to",
            nullable=True,
        )
        if effective_from is None:
            raise SourceCorpusError(f"{source_id} has no reviewed effective_from date")
        if effective_to and effective_to < effective_from:
            raise SourceCorpusError(f"{source_id} effective window is reversed")
        source_hash = hashlib.sha256(raw).hexdigest()
        shared_metadata = {
            "source_id": source_id,
            "source_title": _required_string(source, "title", source_id),
            "source_version_date": _date(
                source.get("source_version_date"),
                context=f"{source_id}.source_version_date",
                nullable=True,
            ),
            "source_last_updated": _date(
                source.get("source_last_updated"),
                context=f"{source_id}.source_last_updated",
                nullable=True,
            ),
            "retrieved_on": retrieved_on,
            "source_sha256": source_hash,
            "source_hash_kind": "sha256_of_local_snapshot_bytes",
            "snapshot_file": str(source_path),
            "capture_kind": "reviewed_source_excerpt",
            "review_status": "reviewed_anchors_verified",
            "date_basis": _required_string(source, "date_basis", source_id),
        }
        authority = _required_string(source, "authority", source_id)
        if authority not in {"contract", "commitment"}:
            raise SourceCorpusError(f"{source_id}.authority must be contract or commitment")
        carrier = source.get("carrier")
        if carrier not in {"AA", "DL", "UA"}:
            raise SourceCorpusError(f"{source_id} carrier must be AA, DL, or UA")
        source_url = _required_string(source, "source_url", source_id)
        if not source_url.startswith("https://"):
            raise SourceCorpusError(f"{source_id}.source_url must use HTTPS")
        if authority == "contract":
            if carrier in contract_carriers:
                raise SourceCorpusError(f"duplicate carrier contract source: {carrier}")
            contract_carriers.add(carrier)
            if shared_metadata["source_version_date"] is None:
                raise SourceCorpusError(f"{source_id} contract needs a dated source version")
        for clause in clauses:
            clause_id = _required_string(clause, "clause_id", source_id)
            document_id = _required_string(clause, "document_id", source_id)
            record = {
                "document_id": document_id,
                "text": _required_string(clause, "text", clause_id),
                "authority": authority,
                "effective_from": effective_from,
                "effective_to": effective_to,
                "source_url": source_url,
                "carrier": carrier,
                "doc_type": authority,
                "metadata": {
                    **shared_metadata,
                    "clause_id": clause_id,
                    "clause_title": _required_string(clause, "title", clause_id),
                    "clause_sha256": hashlib.sha256(
                        _normalized(clause["text"]).encode("utf-8")
                    ).hexdigest(),
                    "scope": clause.get("scope", "").strip(),
                    "source_section": clause.get("source_section", "").strip(),
                },
            }
            records.append(record)
    if contract_carriers != {"AA", "DL", "UA"}:
        missing = sorted({"AA", "DL", "UA"} - contract_carriers)
        raise SourceCorpusError(
            "manifest must contain exactly one dated contract source for AA, DL, and UA; "
            f"missing {missing}"
        )
    ids = [str(record["document_id"]) for record in records]
    if len(ids) != len(set(ids)):
        raise SourceCorpusError("manifest produced duplicate document_id values")
    return records


def _fetch_snapshot(source: Mapping[str, Any], destination: Path) -> None:
    url = _required_string(source, "source_url", "source")
    if not url.startswith("https://"):
        raise SourceCorpusError("source_url must use HTTPS")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Rerouted-source-ingest/0.1 (local research corpus)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise SourceCorpusError(f"source fetch returned HTTP {response.status}: {url}")
            raw = response.read()
    except OSError as exc:
        raise SourceCorpusError(f"could not fetch {url}: {exc}") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("data/rules/source_manifest.json"))
    parser.add_argument("--output", type=Path, default=Path("data/rules/documents.jsonl"))
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="SOURCE_ID=PATH",
        help="use a saved HTML, PDF, or text source instead of its manifest snapshot",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="fetch reviewed URLs to temporary files before verification",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("sources"), list):
            raise SourceCorpusError("manifest.sources must be a list")
        overrides: dict[str, Path] = {}
        for value in args.input:
            source_id, separator, path_value = value.partition("=")
            if not separator or not source_id or not path_value:
                raise SourceCorpusError("--input must be SOURCE_ID=PATH")
            if source_id in overrides:
                raise SourceCorpusError(f"duplicate --input for {source_id}")
            overrides[source_id] = Path(path_value)
        if args.fetch and overrides:
            raise SourceCorpusError("use --fetch or --input, not both")
        if args.fetch:
            with tempfile.TemporaryDirectory(prefix="rerouted-source-capture-") as temp_dir:
                temp_root = Path(temp_dir)
                for source in manifest["sources"]:
                    if not isinstance(source, dict):
                        raise SourceCorpusError("each source must be an object")
                    source_id = _required_string(source, "source_id", "source")
                    suffix = Path(_required_string(source, "snapshot_file", source_id)).suffix
                    fetched_path = temp_root / f"{source_id}{suffix}"
                    _fetch_snapshot(source, fetched_path)
                    overrides[source_id] = fetched_path
                records = ingest_manifest(args.manifest, input_overrides=overrides)
                _write_records(args.output, records)
        else:
            records = ingest_manifest(args.manifest, input_overrides=overrides)
            _write_records(args.output, records)
    except (OSError, SourceCorpusError, json.JSONDecodeError) as exc:
        print(f"source-corpus ingestion failed closed: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {len(records)} reviewed clause records to {args.output}")
    return 0


def _write_records(output: Path, records: Sequence[Mapping[str, Any]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
