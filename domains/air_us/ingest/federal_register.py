"""Review-gated Federal Register enforcement-posture ingestion.

Federal Register API metadata and notice text are source evidence, but neither is
allowed to silently determine the product's legal interpretation.  A human-reviewed
manifest supplies the effect, scope, affected sections, and half-open interval.  The
parser verifies that the metadata and exact date-language anchors still agree with
that review before emitting a posture record.

This module deliberately does not emit ``IndexedDocument`` instances.  Enforcement
posture remains a separate lookup layer and can never rank among rule clauses.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from domains.air_us.rules.posture import PostureNotice, PostureRegistry


class FederalRegisterIngestError(ValueError):
    """Raised when a source snapshot no longer matches its reviewed interpretation."""


@dataclass(frozen=True)
class ReviewedPostureNotice:
    """A verified policy notice with a half-open effective interval."""

    notice_id: str
    document_number: str
    title: str
    publication_date: date
    effect: str
    sections: tuple[str, ...]
    scope: str
    effective_from: date
    effective_to_exclusive: date
    source_url: str

    def in_force(self, as_of_date: date) -> bool:
        return self.effective_from <= as_of_date < self.effective_to_exclusive


def _required_string(mapping: Mapping[str, Any], key: str, *, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FederalRegisterIngestError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _parse_date(value: str, *, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FederalRegisterIngestError(f"{field} must be an ISO date: {value!r}") from exc


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def parse_reviewed_notice(
    payload: Mapping[str, Any],
    notice_text: str,
    review: Mapping[str, Any],
) -> ReviewedPostureNotice:
    """Validate a source snapshot against one reviewed posture entry.

    Intervals come only from ``review`` and are not stretched to meet neighboring
    notices.  ``required_text`` anchors make a changed or incomplete notice snapshot
    fail closed, requiring a new review rather than date inference at query time.
    """

    document_number = _required_string(payload, "document_number", context="payload")
    reviewed_document_number = _required_string(
        review, "document_number", context="review"
    )
    if document_number != reviewed_document_number:
        raise FederalRegisterIngestError(
            "payload document_number does not match the reviewed document"
        )

    source_url = _required_string(payload, "html_url", context="payload")
    reviewed_source_url = _required_string(review, "source_url", context="review")
    if source_url != reviewed_source_url:
        raise FederalRegisterIngestError("payload URL does not match the reviewed source URL")
    if not source_url.startswith("https://www.federalregister.gov/"):
        raise FederalRegisterIngestError("source URL must be an official Federal Register URL")

    publication_date = _parse_date(
        _required_string(payload, "publication_date", context="payload"),
        field="payload.publication_date",
    )
    reviewed_publication_date = review.get("publication_date")
    if reviewed_publication_date is not None and publication_date != _parse_date(
        _required_string(review, "publication_date", context="review"),
        field="review.publication_date",
    ):
        raise FederalRegisterIngestError(
            "payload publication_date does not match the reviewed source date"
        )
    reviewed_title = review.get("title")
    if reviewed_title is not None and _normalize_text(
        _required_string(payload, "title", context="payload")
    ) != _normalize_text(_required_string(review, "title", context="review")):
        raise FederalRegisterIngestError("payload title does not match the reviewed source title")
    effective_from = _parse_date(
        _required_string(review, "effective_from", context="review"),
        field="review.effective_from",
    )
    effective_to_exclusive = _parse_date(
        _required_string(review, "effective_to_exclusive", context="review"),
        field="review.effective_to_exclusive",
    )
    if effective_to_exclusive <= effective_from:
        raise FederalRegisterIngestError("reviewed interval must be non-empty and half-open")
    if effective_from < publication_date:
        raise FederalRegisterIngestError(
            "reviewed enforcement interval cannot begin before notice publication"
        )

    if not isinstance(notice_text, str) or not notice_text.strip():
        raise FederalRegisterIngestError("notice text must be non-empty")
    normalized_text = _normalize_text(notice_text)
    anchors = review.get("required_text")
    if not isinstance(anchors, list) or not anchors:
        raise FederalRegisterIngestError("review.required_text must contain source anchors")
    for anchor in anchors:
        if not isinstance(anchor, str) or not anchor.strip():
            raise FederalRegisterIngestError("review.required_text contains an invalid anchor")
        if _normalize_text(anchor) not in normalized_text:
            raise FederalRegisterIngestError(
                f"notice text is missing reviewed anchor: {anchor!r}"
            )

    sections = review.get("sections")
    if not isinstance(sections, list) or not sections or not all(
        isinstance(section, str) and section.strip() for section in sections
    ):
        raise FederalRegisterIngestError("review.sections must contain section identifiers")

    notice_id = _required_string(review, "notice_id", context="review")
    if notice_id != f"FR-{document_number}":
        raise FederalRegisterIngestError(
            "review.notice_id must be the FR-prefixed document_number"
        )

    return ReviewedPostureNotice(
        notice_id=notice_id,
        document_number=document_number,
        title=_required_string(payload, "title", context="payload"),
        publication_date=publication_date,
        effect=_required_string(review, "effect", context="review"),
        sections=tuple(section.strip() for section in sections),
        scope=_required_string(review, "scope", context="review"),
        effective_from=effective_from,
        effective_to_exclusive=effective_to_exclusive,
        source_url=source_url,
    )


def load_reviewed_notices(manifest_path: Path) -> tuple[ReviewedPostureNotice, ...]:
    """Load local Federal Register snapshots named by a reviewed manifest."""

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FederalRegisterIngestError(f"cannot load review manifest: {manifest_path}") from exc

    reviews = manifest.get("notices") if isinstance(manifest, dict) else None
    if not isinstance(reviews, list):
        raise FederalRegisterIngestError("manifest.notices must be a list")

    results: list[ReviewedPostureNotice] = []
    seen_ids: set[str] = set()
    for review in reviews:
        if not isinstance(review, dict):
            raise FederalRegisterIngestError("each reviewed notice must be an object")
        payload_file = _required_string(review, "payload_file", context="review")
        text_file = _required_string(review, "text_file", context="review")
        try:
            payload = json.loads(
                (manifest_path.parent / payload_file).read_text(encoding="utf-8")
            )
            notice_text = (manifest_path.parent / text_file).read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError) as exc:
            raise FederalRegisterIngestError(
                f"cannot load source snapshot for {review.get('document_number', 'unknown')}"
            ) from exc
        if not isinstance(payload, dict):
            raise FederalRegisterIngestError("Federal Register payload must be an object")
        notice = parse_reviewed_notice(payload, notice_text, review)
        if notice.notice_id in seen_ids:
            raise FederalRegisterIngestError(f"duplicate notice_id: {notice.notice_id}")
        seen_ids.add(notice.notice_id)
        results.append(notice)

    return tuple(sorted(results, key=lambda notice: (notice.effective_from, notice.notice_id)))


def posture_registry_from_reviewed(
    notices: tuple[ReviewedPostureNotice, ...],
    *,
    refresh_required_from: date,
    verified_policy_gap: Mapping[str, str],
) -> PostureRegistry:
    """Adapt reviewed records and gap provenance to the posture lookup layer."""

    gap_from = _parse_date(
        _required_string(
            verified_policy_gap, "effective_from", context="verified_policy_gap"
        ),
        field="verified_policy_gap.effective_from",
    )
    gap_to = _parse_date(
        _required_string(
            verified_policy_gap,
            "effective_to_exclusive",
            context="verified_policy_gap",
        ),
        field="verified_policy_gap.effective_to_exclusive",
    )
    if gap_to <= gap_from:
        raise FederalRegisterIngestError("verified policy gap must be non-empty")
    verification_note = _required_string(
        verified_policy_gap, "verification_note", context="verified_policy_gap"
    )

    return PostureRegistry(
        notices=tuple(
            PostureNotice(
                notice_id=notice.notice_id,
                effect=notice.effect,
                effective_from=notice.effective_from,
                effective_to_exclusive=notice.effective_to_exclusive,
                sections=notice.sections,
                scope=notice.scope,
                source_url=notice.source_url,
            )
            for notice in notices
        ),
        metadata={
            "interval_semantics": "half_open_start_inclusive_end_exclusive",
            "refresh_required_from": refresh_required_from.isoformat(),
            "verified_policy_gap": {
                "effective_from": gap_from.isoformat(),
                "effective_to_exclusive": gap_to.isoformat(),
                "verification_status": verified_policy_gap.get(
                    "verification_status", "reviewed_fixture"
                ),
                "verification_note": verification_note,
            },
        },
    )


def load_reviewed_posture_registry(manifest_path: Path) -> PostureRegistry:
    """Load a complete fixture corpus without dropping reviewed gap metadata."""

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FederalRegisterIngestError(f"cannot load review manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise FederalRegisterIngestError("review manifest must be an object")
    gap = manifest.get("verified_policy_gap")
    if not isinstance(gap, dict):
        raise FederalRegisterIngestError("manifest.verified_policy_gap must be an object")
    refresh_required_from = _parse_date(
        _required_string(
            manifest, "refresh_required_from", context="manifest"
        ),
        field="manifest.refresh_required_from",
    )
    return posture_registry_from_reviewed(
        load_reviewed_notices(manifest_path),
        refresh_required_from=refresh_required_from,
        verified_policy_gap=gap,
    )
