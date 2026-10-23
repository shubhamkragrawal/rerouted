"""Deterministic citation and output audit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core.contracts.state import Citation
from core.retrieval.models import IndexedDocument


@dataclass(frozen=True)
class Claim:
    text: str
    clause_ids: tuple[str, ...]
    load_bearing: bool = True


@dataclass(frozen=True)
class AuditResult:
    accepted_claims: tuple[Claim, ...]
    rejected_claims: tuple[Claim, ...]
    citations: tuple[Citation, ...]
    must_refuse: bool


class GroundingAuditor:
    """Accept only claims supported by a clause active on the requested date."""

    def __init__(self, documents: tuple[IndexedDocument, ...]) -> None:
        by_citation: dict[str, list[IndexedDocument]] = {}
        for document in documents:
            canonical = str(document.metadata.get("clause_id", document.document_id))
            by_citation.setdefault(canonical, []).append(document)
            if canonical != document.document_id:
                by_citation.setdefault(document.document_id, []).append(document)
        self._documents = {
            citation: tuple(sorted(items, key=lambda item: item.effective_from))
            for citation, items in by_citation.items()
        }

    def audit(self, claims: tuple[Claim, ...], *, as_of_date: date) -> AuditResult:
        accepted: list[Claim] = []
        rejected: list[Claim] = []
        citations: dict[str, Citation] = {}
        for claim in claims:
            supporting = []
            for clause_id in claim.clause_ids:
                active = [
                    document
                    for document in self._documents.get(clause_id, ())
                    if document.active_on(as_of_date)
                ]
                # Overlapping versions would make the asserted authority ambiguous.
                if len(active) == 1:
                    supporting.append(active[0])
            if not supporting:
                rejected.append(claim)
                continue
            accepted.append(claim)
            for document in supporting:
                canonical = str(document.metadata.get("clause_id", document.document_id))
                citations[canonical] = Citation(
                    clause_id=canonical,
                    title=str(document.metadata.get("doc", document.document_id)),
                    source_url=document.source_url,
                    authority=document.authority,
                    effective_from=document.effective_from,
                    effective_to=document.effective_to,
                )
        return AuditResult(
            accepted_claims=tuple(accepted),
            rejected_claims=tuple(rejected),
            citations=tuple(citations.values()),
            must_refuse=any(claim.load_bearing for claim in rejected),
        )
