"""Date-filtered retrieval across rule clauses and enforcement posture."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import timedelta

from core.authority import Authority
from core.contracts.tools import (
    EnforcementPosture,
    RuleClause,
    SearchRulesRequest,
    SearchRulesResponse,
)
from core.retrieval import HybridRetriever, IndexedDocument
from domains.air_us.rules.posture import PostureRegistry, load_default_posture


class SearchRulesService:
    """Serve the locked ``search_rules`` contract from local, injected data.

    ``excluded_by_date`` counts clause documents that pass the requested authority
    and carrier predicates but fail their clause effective-date predicate.  Posture
    notices are intentionally not included: they are queried as a separate layer,
    where an empty but checked result is itself meaningful data.
    """

    def __init__(
        self,
        documents: Iterable[IndexedDocument],
        *,
        posture_registry: PostureRegistry | None = None,
        corpus_version: str = "rules-local-fixtures-v1",
    ) -> None:
        substantive_documents = (
            document
            for document in documents
            if document.authority != Authority.ENFORCEMENT_POLICY.value
            and document.doc_type != Authority.ENFORCEMENT_POLICY.value
        )
        self._retriever = HybridRetriever(substantive_documents)
        self._posture_registry = posture_registry or load_default_posture()
        self._corpus_version = corpus_version

    def search(self, request: SearchRulesRequest) -> SearchRulesResponse:
        """Search applicable clauses, then independently look up date posture."""

        requested_types = {
            authority.value
            for authority in request.doc_types
            if authority is not Authority.ENFORCEMENT_POLICY
        }
        # An empty doc_types list means all substantive clause authorities.  If the
        # caller requests only enforcement policy, no clauses should be searched.
        clause_types = requested_types or (
            {
                Authority.REGULATION.value,
                Authority.COMMITMENT.value,
                Authority.CONTRACT.value,
            }
            if not request.doc_types
            else {"__no_clause_authority__"}
        )
        retrieval = self._retriever.search_detailed(
            request.query,
            as_of_date=request.as_of_date,
            k=request.k,
            doc_types=clause_types,
            carrier=request.carrier,
        )

        # Request-scoped evidence completion queries name the resolver's stable
        # clause IDs. Those IDs are metadata in the corpus rather than clause text,
        # so lexical retrieval alone cannot reliably find them. Resolve only those
        # explicit IDs from the same indexed corpus and keep normal retrieval as the
        # bounded fallback/fill for the requested result count.
        targeted_ids = set(
            re.findall(r"\b\d+CFR\d+(?:\.\d+)?(?:\([a-zA-Z0-9]+\))*", request.query)
        )
        targeted_documents = [
            document
            for document in self._retriever.documents
            if str(document.metadata.get("clause_id", document.document_id)) in targeted_ids
            and document.active_on(request.as_of_date)
            and document.carrier in (None, request.carrier)
            and document.authority in clause_types
        ]
        targeted_documents.sort(
            key=lambda document: str(
                document.metadata.get("clause_id", document.document_id)
            )
        )
        selected_documents = list(targeted_documents[: request.k])
        selected_ids = {
            str(document.metadata.get("clause_id", document.document_id))
            for document in selected_documents
        }
        for hit in retrieval.hits:
            clause_id = str(hit.document.metadata.get("clause_id", hit.document.document_id))
            if clause_id not in selected_ids and len(selected_documents) < request.k:
                selected_documents.append(hit.document)
                selected_ids.add(clause_id)
        selected_hits = {id(hit.document): hit for hit in retrieval.hits}
        clauses = tuple(
            RuleClause(
                clause_id=str(document.metadata.get("clause_id", document.document_id)),
                doc=str(document.metadata.get("doc", document.document_id)),
                text=document.text,
                effective_from=document.effective_from,
                effective_to=document.effective_to,
                authority=Authority(document.authority),
                score=(selected_hits[id(document)].score if id(document) in selected_hits else 1.0),
                rerank_score=(
                    selected_hits[id(document)].rerank_score
                    if id(document) in selected_hits
                    else 1.0
                ),
                source_url=document.source_url,
            )
            for document in selected_documents
        )

        posture_lookup = self._posture_registry.lookup(request.as_of_date)
        posture = tuple(
            EnforcementPosture(
                fr_doc=notice.notice_id,
                effect=notice.effect,
                sections=notice.sections,
                scope=notice.scope,
                effective_from=notice.effective_from,
                # Public contract uses an inclusive end; registry storage is half-open.
                effective_to=notice.effective_to_exclusive - timedelta(days=1),
                source_url=notice.source_url,
            )
            for notice in posture_lookup.applicable_notices
        )

        return SearchRulesResponse(
            clauses=clauses,
            posture=posture,
            posture_checked=posture_lookup.checked,
            as_of_date=request.as_of_date,
            excluded_by_date=retrieval.report.excluded_by_date,
            corpus_version=self._corpus_version,
            retrieval_provider=retrieval.report.served_provider,
            retrieval_model=retrieval.report.served_model_id,
            retrieval_fallback_reason=retrieval.report.fallback_reason,
        )


def search_rules(
    request: SearchRulesRequest,
    *,
    documents: Iterable[IndexedDocument],
    posture_registry: PostureRegistry | None = None,
    corpus_version: str = "rules-local-fixtures-v1",
) -> SearchRulesResponse:
    """Functional entry point for local integrations and the future tool router."""

    return SearchRulesService(
        documents,
        posture_registry=posture_registry,
        corpus_version=corpus_version,
    ).search(request)
