"""Dependency-free, citation-boundary ingestion for eCFR XML snapshots.

The eCFR versioner exposes complete XML snapshots.  This module deliberately keeps
the date a snapshot was queried separate from the effective date of the eCFR version
it contains.  Each supplied version is a complete view of a part: a clause absent
from the next version is closed at that version's effective date.  Effective
intervals are half-open, ``[effective_from, effective_to)``, and repeated observations
of one unchanged version extend one interval instead of manufacturing a new version.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

_SPACE_RE = re.compile(r"\s+")
_SECTION_RE = re.compile(r"(?:§+\s*)?(\d+\.\d+[A-Za-z0-9.-]*)")
_PARAGRAPH_PREFIX_RE = re.compile(
    r"^\s*((?:\([A-Za-z0-9ivxlcdmIVXLCDM]+\))+)(?:\s*|$)"
)
_PARAGRAPH_TOKEN_RE = re.compile(r"\(([A-Za-z0-9ivxlcdmIVXLCDM]+)\)")
_ROMAN_LOWER = frozenset("ivxlcdm")
_ROMAN_UPPER = frozenset("IVXLCDM")


def _local_name(tag: str) -> str:
    """Return an XML tag without a namespace prefix."""

    return tag.rsplit("}", 1)[-1].upper()


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return _SPACE_RE.sub(" ", "".join(element.itertext())).strip()


def _first_child_text(element: ET.Element, tag: str) -> str:
    wanted = tag.upper()
    for child in element:
        if _local_name(child.tag) == wanted:
            return _text(child)
    return ""


def _first_text(root: ET.Element, tag: str) -> str:
    wanted = tag.upper()
    for element in root.iter():
        if _local_name(element.tag) == wanted:
            value = _text(element)
            if value:
                return value
    return ""


def _canonical_hash(
    *, heading: str, ancestors: tuple[str, ...], text: str
) -> str:
    payload = json.dumps(
        {
            "heading": _SPACE_RE.sub(" ", heading).strip(),
            "ancestors": [_SPACE_RE.sub(" ", item).strip() for item in ancestors],
            "text": _SPACE_RE.sub(" ", text).strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EcfrSnapshot:
    """One complete eCFR part snapshot and both dates in its provenance."""

    version_effective_on: date
    queried_on: date
    xml_text: str
    source_url: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.queried_on < self.version_effective_on:
            raise ValueError("queried_on cannot precede version_effective_on")


@dataclass(frozen=True)
class EcfrClause:
    """A citable paragraph or section-level definition block from one snapshot."""

    document_id: str
    clause_id: str
    title: int
    part: str
    section: str
    paragraph_path: tuple[str, ...]
    heading: str
    text: str
    ancestor_text: tuple[str, ...]
    text_hash: str
    version_effective_on: date
    queried_on: date
    source_url: str
    document_metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def hierarchy(self) -> tuple[str, ...]:
        """Return reconstructable identifiers from title down to the leaf."""

        base = (f"Title {self.title}", f"Part {self.part}", f"§ {self.section}")
        return base + tuple(f"({token})" for token in self.paragraph_path)


@dataclass(frozen=True)
class EcfrClauseInterval:
    """A content-stable, half-open effective interval for one leaf clause."""

    document_id: str
    clause_id: str
    title: int
    part: str
    section: str
    paragraph_path: tuple[str, ...]
    heading: str
    text: str
    ancestor_text: tuple[str, ...]
    text_hash: str
    effective_from: date
    effective_to: date | None
    source_queried_on: date
    source_url: str
    document_metadata: Mapping[str, Any] = field(default_factory=dict)

    def active_on(self, as_of_date: date) -> bool:
        """Apply half-open ``[from, to)`` date semantics."""

        return self.effective_from <= as_of_date and (
            self.effective_to is None or as_of_date < self.effective_to
        )

    def to_indexed_document(self) -> Any:
        """Convert to the repository's retrieval record without changing semantics.

        ``IndexedDocument`` currently uses inclusive date endpoints, while ingestion
        intervals are half-open.  Since this corpus is day-granular, an exclusive end
        of March 14 maps to an inclusive end of March 13.  The original exclusive end
        is also retained in metadata so the source interval remains auditable.
        """

        from core.retrieval.models import IndexedDocument

        metadata = dict(self.document_metadata)
        metadata.update(
            {
                "clause_id": self.clause_id,
                "cfr_title": self.title,
                "cfr_part": self.part,
                "cfr_section": self.section,
                "paragraph_path": self.paragraph_path,
                "heading": self.heading,
                "ancestor_text": self.ancestor_text,
                "text_hash": self.text_hash,
                "effective_to_exclusive": (
                    self.effective_to.isoformat() if self.effective_to else None
                ),
                "source_queried_on": self.source_queried_on.isoformat(),
            }
        )
        return IndexedDocument(
            document_id=self.document_id,
            text=self.text,
            authority="regulation",
            effective_from=self.effective_from,
            effective_to=(
                self.effective_to - timedelta(days=1) if self.effective_to else None
            ),
            source_url=self.source_url,
            doc_type="regulation",
            metadata=metadata,
        )


@dataclass(frozen=True)
class _Paragraph:
    path: tuple[str, ...]
    text: str


def _section_number(section: ET.Element) -> str | None:
    for candidate in (section.attrib.get("N", ""), _first_child_text(section, "HEAD")):
        match = _SECTION_RE.search(candidate)
        if match:
            return match.group(1)
    return None


def _part_number(root: ET.Element) -> str | None:
    for element in root.iter():
        if element.attrib.get("TYPE", "").upper() != "PART":
            continue
        candidate = element.attrib.get("N", "")
        match = re.search(r"\d+[A-Za-z0-9.-]*", candidate)
        if match:
            return match.group(0)
    return None


def _is_section(element: ET.Element) -> bool:
    if element.attrib.get("TYPE", "").upper() == "SECTION":
        return True
    return (
        _local_name(element.tag) in {"SECTION", "SECTNO"}
        and _section_number(element) is not None
    )


def _infer_level(token: str, current: list[str]) -> int:
    """Infer the conventional CFR paragraph depth for a single label.

    CFR paragraph labels conventionally progress ``(a)(1)(i)(A)(I)``.  Lowercase
    roman labels are ambiguous with alphabetic labels, so they are treated as roman
    only when the current context already contains a numeric parent.
    """

    if token.isdigit():
        return 1
    if token.islower():
        # Flat eCFR XML makes a top-level ``(c)`` after ``(b)(6)`` look like a
        # lowercase Roman child. Prefer the ordinary alphabetic sequence when the
        # token advances the current top-level letter; otherwise a Roman token
        # following a numeric parent is the conventional third level.
        if (
            len(token) == 1
            and current
            and len(current[0]) == 1
            and ord(token) == ord(current[0]) + 1
        ):
            return 0
        if (
            set(token) <= _ROMAN_LOWER
            and len(current) >= 2
            and current[1].isdigit()
        ):
            return 2
        return 0
    if token.isupper():
        if set(token) <= _ROMAN_UPPER and len(current) >= 4:
            return 4
        return 3
    return len(current)


def _leading_path(element: ET.Element, current: list[str]) -> tuple[str, ...]:
    """Return the official leading paragraph path for one eCFR ``P`` element.

    The production eCFR XML sometimes encodes two levels in one paragraph as
    ``(a) <I>Heading</I>—(1) <I>Subheading</I> text``.  Flattening ``itertext``
    hides that boundary and previously shortened the citation to ``(a)``.  The
    child-tail check below preserves the XML semantics without guessing from
    arbitrary prose that happens to contain a parenthesized number.
    """

    value = _text(element)
    match = _PARAGRAPH_PREFIX_RE.match(value)
    if match is None:
        return ()
    tokens = tuple(_PARAGRAPH_TOKEN_RE.findall(match.group(1)))
    if len(tokens) == 1 and tokens[0].islower():
        children = list(element)
        if children and _local_name(children[0].tag) in {"I", "EM"}:
            tail = children[0].tail or ""
            nested = re.match(r"^\s*[—–-]?\s*\((\d+)\)", tail)
            if nested:
                tokens = (tokens[0], nested.group(1))
    if len(tokens) > 1:
        current[:] = tokens
        return tokens
    level = _infer_level(tokens[0], current)
    current[:] = current[:level] + [tokens[0]]
    return tuple(current)


def _paragraphs(section: ET.Element) -> list[_Paragraph]:
    raw: list[tuple[tuple[str, ...], str]] = []
    current: list[str] = []
    for element in section.iter():
        if _local_name(element.tag) != "P":
            continue
        value = _text(element)
        if not value:
            continue
        path = _leading_path(element, current)
        raw.append((path, value))

    # Definition sections commonly contain several unnumbered P elements.  They all
    # share one official section citation, so group them into one stable section-level
    # retrieval record instead of inventing ordinal citations such as ``#p2``.
    if raw and all(not path for path, _ in raw):
        return [_Paragraph(path=(), text="\n\n".join(value for _, value in raw))]

    # Parent paragraphs can carry operative text independent of their children
    # (for example 14 CFR 260.6(a)(1)). Keep every explicit official citation.
    # Multiple XML P nodes can continue the same citation (especially an
    # unnumbered section-level preamble), so coalesce those nodes deterministically.
    grouped: dict[tuple[str, ...], list[str]] = {}
    for path, value in raw:
        grouped.setdefault(path, []).append(value)
    return [
        _Paragraph(path=path, text="\n\n".join(values))
        for path, values in grouped.items()
    ]


def _ancestor_texts(section: ET.Element) -> dict[tuple[str, ...], str]:
    """Collect text for paragraph prefixes so leaf chunks retain their context."""

    result: dict[tuple[str, ...], str] = {}
    current: list[str] = []
    for element in section.iter():
        if _local_name(element.tag) != "P":
            continue
        value = _text(element)
        if not value:
            continue
        path = _leading_path(element, current)
        if not path:
            if value:
                result[()] = "\n\n".join(filter(None, (result.get((), ""), value)))
            continue
        result[path] = value
    return result


def parse_ecfr_xml(
    xml_text: str,
    *,
    version_effective_on: date,
    queried_on: date,
    source_url: str,
    title: int = 14,
    part: str | None = None,
    document_metadata: Mapping[str, Any] | None = None,
) -> tuple[EcfrClause, ...]:
    """Parse a complete eCFR XML snapshot into citable paragraph records."""

    if queried_on < version_effective_on:
        raise ValueError("queried_on cannot precede version_effective_on")

    root = ET.fromstring(xml_text)
    resolved_part = part or _part_number(root)
    if not resolved_part:
        raise ValueError("could not determine CFR part from XML; pass part explicitly")

    extracted_metadata: dict[str, Any] = {
        "document_title": _first_text(root, "HEAD"),
        "authority": _first_text(root, "AUTH"),
        "source_note": _first_text(root, "SOURCE"),
    }
    extracted_metadata.update(document_metadata or {})
    # Provenance derived from source bytes cannot be overridden by caller metadata.
    extracted_metadata["snapshot_sha256"] = hashlib.sha256(
        xml_text.encode("utf-8")
    ).hexdigest()
    extracted_metadata["version_effective_on"] = version_effective_on.isoformat()
    extracted_metadata["queried_on"] = queried_on.isoformat()
    extracted_metadata = {key: value for key, value in extracted_metadata.items() if value != ""}

    clauses: list[EcfrClause] = []
    seen_document_ids: set[str] = set()
    for section in root.iter():
        if not _is_section(section):
            continue
        section_number = _section_number(section)
        if section_number is None:
            continue
        heading = _first_child_text(section, "HEAD")
        context = _ancestor_texts(section)
        paragraphs = _paragraphs(section)
        # Definition sections mix unlabelled term definitions with labelled lists
        # inside individual definitions. Those list labels are not section-level
        # paragraph citations, so expose one auditable section citation.
        if "definition" in heading.casefold():
            values = [
                _text(element)
                for element in section.iter()
                if _local_name(element.tag) == "P"
            ]
            paragraphs = [_Paragraph(path=(), text="\n\n".join(filter(None, values)))]
        for paragraph in paragraphs:
            suffix = "".join(f"({token})" for token in paragraph.path)
            clause_id = f"{title}CFR{section_number}{suffix}"
            # Unnumbered paragraphs are grouped above, so the official citation is a
            # stable, collision-free internal document key as well.
            document_id = clause_id
            if document_id in seen_document_ids:
                raise ValueError(f"duplicate document id in snapshot: {document_id}")
            seen_document_ids.add(document_id)
            ancestors = tuple(
                context[prefix]
                for depth in range(len(paragraph.path))
                if (prefix := paragraph.path[:depth]) in context
            )
            clauses.append(
                EcfrClause(
                    document_id=document_id,
                    clause_id=clause_id,
                    title=title,
                    part=resolved_part,
                    section=section_number,
                    paragraph_path=paragraph.path,
                    heading=heading,
                    text=paragraph.text,
                    ancestor_text=ancestors,
                    text_hash=_canonical_hash(
                        heading=heading, ancestors=ancestors, text=paragraph.text
                    ),
                    version_effective_on=version_effective_on,
                    queried_on=queried_on,
                    source_url=source_url,
                    document_metadata=dict(extracted_metadata),
                )
            )
    if not clauses:
        raise ValueError("no eCFR leaf paragraphs found")
    return tuple(sorted(clauses, key=lambda clause: clause.document_id))


def _open_interval(clause: EcfrClause) -> EcfrClauseInterval:
    return EcfrClauseInterval(
        document_id=clause.document_id,
        clause_id=clause.clause_id,
        title=clause.title,
        part=clause.part,
        section=clause.section,
        paragraph_path=clause.paragraph_path,
        heading=clause.heading,
        text=clause.text,
        ancestor_text=clause.ancestor_text,
        text_hash=clause.text_hash,
        effective_from=clause.version_effective_on,
        effective_to=None,
        source_queried_on=clause.queried_on,
        source_url=clause.source_url,
        document_metadata=clause.document_metadata,
    )


def build_effective_intervals(
    snapshots: Iterable[EcfrSnapshot],
    *,
    title: int = 14,
    part: str | None = None,
) -> tuple[EcfrClauseInterval, ...]:
    """Build content-stable intervals from complete chronological snapshots.

    Multiple query observations may point to the same version-effective date.  Those
    observations must contain identical clauses.  A changed or removed clause closes
    at the next version's effective date.  An unchanged clause remains open and
    retains the provenance of the first observation of that effective version.
    """

    ordered = sorted(
        snapshots,
        key=lambda snapshot: (snapshot.version_effective_on, snapshot.queried_on),
    )
    if not ordered:
        return ()

    versions: list[tuple[date, tuple[EcfrClause, ...]]] = []
    version_signatures: dict[date, tuple[tuple[str, str], ...]] = {}
    for snapshot in ordered:
        parsed = parse_ecfr_xml(
            snapshot.xml_text,
            version_effective_on=snapshot.version_effective_on,
            queried_on=snapshot.queried_on,
            source_url=snapshot.source_url,
            title=title,
            part=part,
            document_metadata=snapshot.metadata,
        )
        signature = tuple((clause.document_id, clause.text_hash) for clause in parsed)
        prior_signature = version_signatures.get(snapshot.version_effective_on)
        if prior_signature is not None:
            if prior_signature != signature:
                raise ValueError(
                    "conflicting snapshots for version effective date "
                    f"{snapshot.version_effective_on.isoformat()}"
                )
            continue
        version_signatures[snapshot.version_effective_on] = signature
        versions.append((snapshot.version_effective_on, parsed))

    active: dict[str, EcfrClauseInterval] = {}
    completed: list[EcfrClauseInterval] = []
    for version_effective_on, parsed in versions:
        current = {clause.document_id: clause for clause in parsed}

        for document_id in set(active) - set(current):
            completed.append(
                replace(
                    active.pop(document_id), effective_to=version_effective_on
                )
            )

        for document_id, clause in current.items():
            prior = active.get(document_id)
            if prior is None:
                active[document_id] = _open_interval(clause)
            elif prior.text_hash != clause.text_hash:
                completed.append(replace(prior, effective_to=version_effective_on))
                active[document_id] = _open_interval(clause)

    completed.extend(active.values())
    return tuple(
        sorted(
            completed,
            key=lambda interval: (interval.document_id, interval.effective_from),
        )
    )
