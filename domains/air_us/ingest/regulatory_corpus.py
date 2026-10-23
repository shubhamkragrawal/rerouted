"""Fetch and merge official eCFR and reviewed Federal Register snapshots.

The command is intentionally local-first: source bytes are saved below
``data/rules/sources`` and the resulting regulation records are merged into the
existing contract/commitment index only after all sources pass validation.
Federal Register notices are validated against the reviewed posture manifest but
are never emitted as ranked rule clauses.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, cast

from domains.air_us.ingest.ecfr import (
    EcfrClauseInterval,
    EcfrSnapshot,
    build_effective_intervals,
)
from domains.air_us.ingest.federal_register import (
    FederalRegisterIngestError,
    parse_reviewed_notice,
)

ECFR_BASE = "https://www.ecfr.gov/api/versioner/v1"
FEDERAL_REGISTER_API = "https://www.federalregister.gov/api/v1/documents"
NOTICE_IDS = ("2025-22140", "2026-13675")
ECFR_TARGETS = ((260, None), (259, None), (399, "399.80"))
USER_AGENT = "Rerouted-regulatory-corpus/0.1 (local research corpus)"


class RegulatoryCorpusError(ValueError):
    """Raised when an official source or its schema fails closed validation."""


def _read_url(url: str, *, max_bytes: int = 40_000_000) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "www.ecfr.gov",
        "www.federalregister.gov",
        "federalregister.gov",
    }:
        raise RegulatoryCorpusError(f"refusing non-authoritative URL: {url}")
    request = urllib.request.Request(
        url,
        # Both APIs select the representation from the URL suffix. eCFR requires
        # clients to accept compression and otherwise returns HTTP 406.
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                raise RegulatoryCorpusError(f"source returned HTTP {response.status}: {url}")
            raw = cast(bytes, response.read(max_bytes + 1))
            headers = getattr(response, "headers", {})
            content_encoding = headers.get("Content-Encoding", "")
    except (OSError, urllib.error.URLError) as exc:
        raise RegulatoryCorpusError(f"could not fetch {url}: {exc}") from exc
    if content_encoding.casefold() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            raise RegulatoryCorpusError(f"source returned invalid gzip data: {url}") from exc
    if len(raw) > max_bytes:
        raise RegulatoryCorpusError(f"source exceeded {max_bytes} bytes: {url}")
    if not raw:
        raise RegulatoryCorpusError(f"source returned an empty body: {url}")
    return raw


def _json_response(url: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(_read_url(url).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegulatoryCorpusError(f"source did not return valid UTF-8 JSON: {url}") from exc
    if not isinstance(payload, dict):
        raise RegulatoryCorpusError(f"source JSON root must be an object: {url}")
    return payload


def _version_records(
    part: int, start_date: date, query_date: date, section: str | None = None
) -> tuple[Mapping[str, Any], ...]:
    """Read all official version rows for one part across the requested horizon."""

    rows: list[Mapping[str, Any]] = []
    page = 1
    while True:
        params = {
            "part": str(part),
            "issue_date[gte]": start_date.isoformat(),
            "issue_date[lte]": query_date.isoformat(),
            "page": str(page),
        }
        if section is not None:
            params["section"] = section
        query = urllib.parse.urlencode(params)
        payload = _json_response(f"{ECFR_BASE}/versions/title-14.json?{query}")
        page_rows = payload.get("content_versions")
        if not isinstance(page_rows, list):
            raise RegulatoryCorpusError("eCFR versions response missing content_versions list")
        if not all(isinstance(item, dict) for item in page_rows):
            raise RegulatoryCorpusError("eCFR content_versions contains a non-object row")
        rows.extend(page_rows)
        if not page_rows or len(page_rows) < 1000:
            break
        page += 1
    return tuple(rows)


def _version_dates(
    rows: Sequence[Mapping[str, Any]], *, part: int, start_date: date, query_date: date
) -> tuple[date, ...]:
    dates: set[date] = set()
    for row in rows:
        row_part = row.get("part")
        if str(row_part) != str(part):
            continue
        raw_date = row.get("date")
        amendment_date = row.get("amendment_date")
        if not isinstance(raw_date, str) or not isinstance(amendment_date, str):
            raise RegulatoryCorpusError("eCFR version row is missing date/amendment_date")
        try:
            effective_date = date.fromisoformat(raw_date)
            amendment = date.fromisoformat(amendment_date)
        except ValueError as exc:
            raise RegulatoryCorpusError("eCFR version row contains a malformed date") from exc
        # The Versioner schema defines `date` as the effective version date.  Require
        # the deprecated date field and amendment_date to agree, so schema changes do
        # not silently shift interval boundaries.
        if effective_date != amendment:
            raise RegulatoryCorpusError("eCFR date and amendment_date no longer agree")
        if start_date <= effective_date <= query_date:
            dates.add(effective_date)
    return tuple(sorted(dates))


def _ecfr_url(version_date: date, part: int) -> str:
    query = urllib.parse.urlencode({"part": str(part)})
    return f"{ECFR_BASE}/full/{version_date.isoformat()}/title-14.xml?{query}"


def fetch_ecfr_intervals(
    *,
    start_date: date,
    query_date: date,
    retrieved_on: date,
    sources_dir: Path,
) -> tuple[Any, ...]:
    """Fetch exact amendment-date snapshots and parse them with the existing parser."""

    if query_date < start_date:
        raise RegulatoryCorpusError("query date cannot precede historical start date")
    if retrieved_on < query_date:
        raise RegulatoryCorpusError("retrieval date cannot precede the eCFR query cutoff")
    snapshots_by_part: dict[int, list[EcfrClauseInterval]] = {}
    for part, required_section in ECFR_TARGETS:
        rows = _version_records(part, start_date, query_date, section=required_section)
        # A part can be unchanged throughout the requested window and therefore
        # have no version rows. Always capture the authoritative view at the
        # requested start date, then add every amendment boundary in the window.
        boundaries = tuple(
            sorted(
                {
                    start_date,
                    *_version_dates(
                        rows,
                        part=part,
                        start_date=start_date,
                        query_date=query_date,
                    ),
                }
            )
        )
        snapshots: list[EcfrSnapshot] = []
        for version_date in boundaries:
            url = _ecfr_url(version_date, part)
            raw = _read_url(url)
            try:
                xml_text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise RegulatoryCorpusError(f"eCFR XML is not UTF-8: {url}") from exc
            snapshot = EcfrSnapshot(
                version_effective_on=version_date,
                queried_on=retrieved_on,
                xml_text=xml_text,
                source_url=url,
                metadata={
                    "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                    "source_hash_kind": "sha256_of_ecfr_xml_snapshot_bytes",
                    "snapshot_as_of_on": version_date.isoformat(),
                    "source_snapshot_file": (
                        f"ecfr_title14_part{part}_asof_{version_date.isoformat()}.xml"
                    ),
                },
            )
            snapshots.append(snapshot)
            destination = (
                sources_dir / f"ecfr_title14_part{part}_asof_{version_date.isoformat()}.xml"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        parsed_intervals = build_effective_intervals(snapshots, title=14, part=str(part))
        if required_section:
            parsed_intervals = tuple(
                item for item in parsed_intervals if item.section == required_section
            )
            if not parsed_intervals:
                raise RegulatoryCorpusError(
                    f"eCFR Part {part} did not contain required section {required_section}"
                )
        snapshots_by_part[part] = list(parsed_intervals)
    return tuple(item for part in (260, 259, 399) for item in snapshots_by_part[part])


def _review_manifest(path: Path) -> tuple[Mapping[str, Any], ...]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegulatoryCorpusError(f"cannot read reviewed posture manifest: {path}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise RegulatoryCorpusError("reviewed posture manifest schema_version must be 1")
    notices = manifest.get("notices")
    if not isinstance(notices, list):
        raise RegulatoryCorpusError("reviewed posture manifest notices must be a list")
    by_number = {item.get("document_number"): item for item in notices if isinstance(item, dict)}
    if len(by_number) != len(notices) or set(by_number) != set(NOTICE_IDS):
        raise RegulatoryCorpusError("reviewed posture manifest notice IDs drifted")
    return tuple(by_number[number] for number in NOTICE_IDS)


def _notice_text_url(payload: Mapping[str, Any]) -> str:
    for field in ("raw_text_url", "body_html_url"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise RegulatoryCorpusError("Federal Register payload missing raw_text_url/body_html_url")


def fetch_and_validate_notices(
    *, review_manifest: Path
) -> tuple[tuple[Mapping[str, Any], str, bytes, bytes], ...]:
    """Fetch notice metadata/text and validate against reviewed posture entries."""

    notices: list[tuple[Mapping[str, Any], str, bytes, bytes]] = []
    for review in _review_manifest(review_manifest):
        number = str(review["document_number"])
        payload_url = f"{FEDERAL_REGISTER_API}/{number}.json"
        payload_raw = _read_url(payload_url, max_bytes=5_000_000)
        try:
            payload = json.loads(payload_raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegulatoryCorpusError(
                f"Federal Register metadata is invalid JSON: {number}"
            ) from exc
        if not isinstance(payload, dict):
            raise RegulatoryCorpusError(f"Federal Register payload must be an object: {number}")
        text_url = _notice_text_url(payload)
        text_raw = _read_url(text_url, max_bytes=20_000_000)
        try:
            notice_text = text_raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise RegulatoryCorpusError(
                f"Federal Register notice text is not UTF-8: {number}"
            ) from exc
        if text_url.endswith((".html", ".htm")):
            from domains.air_us.ingest.source_corpus import normalize_source

            notice_text = normalize_source(Path("notice.html"), text_raw)
        try:
            parsed = parse_reviewed_notice(payload, notice_text, review)
        except FederalRegisterIngestError as exc:
            raise RegulatoryCorpusError(f"Federal Register source failed review: {exc}") from exc
        if parsed.document_number != number:
            raise RegulatoryCorpusError("Federal Register response document number drifted")
        notices.append((payload, notice_text, payload_raw, text_raw))
    return tuple(notices)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RegulatoryCorpusError(f"cannot read existing corpus: {path}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RegulatoryCorpusError(f"invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("document_id"), str):
            raise RegulatoryCorpusError(f"invalid document record at {path}:{line_number}")
        records.append(value)
    ids = [record["document_id"] for record in records]
    if len(ids) != len(set(ids)):
        raise RegulatoryCorpusError("existing corpus contains duplicate document_id values")
    return records


def merge_records(
    existing: Sequence[Mapping[str, Any]], additions: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Merge records idempotently; mismatched duplicate IDs fail closed."""

    merged = [dict(record) for record in existing]
    known = {record["document_id"]: record for record in merged}
    for addition in additions:
        document_id = addition.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise RegulatoryCorpusError("new record has no document_id")
        previous = known.get(document_id)
        if previous is not None:
            if previous != dict(addition):
                raise RegulatoryCorpusError(
                    f"document_id collision with different content: {document_id}"
                )
            continue
        record = dict(addition)
        merged.append(record)
        known[document_id] = record
    return merged


def _records_from_intervals(intervals: Sequence[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for interval in intervals:
        indexed = interval.to_indexed_document()
        metadata = dict(indexed.metadata)
        metadata.update(
            {
                "source_sha256": metadata.get("source_sha256", metadata["snapshot_sha256"]),
                "source_hash_kind": "sha256_of_ecfr_xml_snapshot_bytes",
                "snapshot_file": (
                    "data/rules/sources/"
                    + metadata.get(
                        "source_snapshot_file",
                        f"ecfr_title14_part{interval.part}_asof_"
                        f"{interval.effective_from.isoformat()}.xml",
                    )
                ),
                "version_effective_on": interval.effective_from.isoformat(),
            }
        )
        records.append(
            {
                "document_id": f"{indexed.document_id}@{interval.effective_from.isoformat()}",
                "text": indexed.text,
                "authority": "regulation",
                "effective_from": indexed.effective_from.isoformat(),
                "effective_to": indexed.effective_to.isoformat() if indexed.effective_to else None,
                "source_url": indexed.source_url,
                "carrier": None,
                "doc_type": "regulation",
                "metadata": metadata,
            }
        )
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--retrieved-on", type=date.fromisoformat, default=date.today())
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(1996, 1, 1))
    parser.add_argument("--output", type=Path, default=Path("data/rules/documents.jsonl"))
    parser.add_argument(
        "--review-manifest",
        type=Path,
        default=Path("domains/air_us/posture.yaml"),
    )
    parser.add_argument("--sources-dir", type=Path, default=Path("data/rules/sources"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.query_date > args.retrieved_on:
            raise RegulatoryCorpusError("query date cannot be in the future")
        with tempfile.TemporaryDirectory(prefix="rerouted-regulatory-capture-") as temp_dir:
            staged_sources = Path(temp_dir) / "sources"
            intervals = fetch_ecfr_intervals(
                start_date=args.start_date,
                query_date=args.query_date,
                retrieved_on=args.retrieved_on,
                sources_dir=staged_sources,
            )
            notices = fetch_and_validate_notices(
                review_manifest=args.review_manifest,
            )
            # Save Federal Register source snapshots only after review succeeds.
            notice_provenance: list[dict[str, str]] = []
            for payload, notice_text, payload_raw, text_raw in notices:
                number = str(payload["document_number"])
                (staged_sources / f"federal_register_{number}.json").write_bytes(payload_raw)
                (staged_sources / f"federal_register_{number}.txt").write_text(
                    notice_text, encoding="utf-8"
                )
                (staged_sources / f"federal_register_{number}.source").write_bytes(text_raw)
                (staged_sources / f"federal_register_{number}.text-source.sha256").write_text(
                    hashlib.sha256(text_raw).hexdigest() + "\n", encoding="ascii"
                )
                notice_provenance.append(
                    {
                        "document_number": number,
                        "metadata_url": f"{FEDERAL_REGISTER_API}/{number}.json",
                        "metadata_sha256": hashlib.sha256(payload_raw).hexdigest(),
                        "text_url": _notice_text_url(payload),
                        "text_snapshot_file": f"federal_register_{number}.source",
                        "text_sha256": hashlib.sha256(text_raw).hexdigest(),
                    }
                )
            (staged_sources / "federal_register_capture_manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "query_date": args.query_date.isoformat(),
                        "retrieved_on": args.retrieved_on.isoformat(),
                        "notices": notice_provenance,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            additions = _records_from_intervals(intervals)
            # This command owns the official regulation slice. Rebuild that slice
            # atomically so parser fixes and source updates cannot leave stale rows,
            # while preserving independently sourced contracts and commitments.
            existing = [
                record
                for record in _read_jsonl(args.output)
                if record.get("authority") != "regulation"
            ]
            merged = merge_records(existing, additions)
            args.sources_dir.mkdir(parents=True, exist_ok=True)
            for source_path in staged_sources.iterdir():
                (args.sources_dir / source_path.name).write_bytes(source_path.read_bytes())
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                "".join(
                    json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in merged
                ),
                encoding="utf-8",
            )
    except (OSError, ValueError, RegulatoryCorpusError) as exc:
        print(f"regulatory corpus ingestion failed closed: {exc}", file=sys.stderr)
        return 2
    print(
        f"merged {len(additions)} official eCFR clause intervals; "
        f"validated {len(notices)} separate Federal Register posture notices"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
