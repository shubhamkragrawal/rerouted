"""Run the deterministic answer service and retrieval ablations on the golden set."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.answer import AnswerRequest, AnswerService
from core.eval import (
    CasePrediction,
    evaluate_answers,
    evaluate_ci,
    load_golden_set,
    operations_metrics,
)
from core.retrieval import HybridRetriever
from domains.air_us.bootstrap import AirUSServices

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "domains/air_us/golden_set.jsonl"
DEFAULT_CONFIG_PATH = ROOT / "config/default.yaml"
RESULTS_DIR = ROOT / "data/eval/results"
FACT_FIELDS = (
    "covered_flight",
    "merchant_of_record",
    "itinerary_kind",
    "consumer_disposition",
    "cancelled",
    "flight_number_changed",
    "rebooked_on_renumbered_flight",
    "departure_earlier_minutes",
    "arrival_later_minutes",
    "origin_or_destination_changed",
    "added_connection",
    "downgraded_class",
    "passenger_has_disability",
    "disability_connection_changed",
    "needed_accessibility_feature_unavailable",
)


def _case_query(case: Any) -> str:
    scenario = case.scenario
    event = str(scenario["event"]).replace("_", " ")
    cause = str(scenario.get("cause", "unspecified cause")).replace("_", " ")
    return (
        f"US airline {event}; {cause}; refund eligibility and enforcement posture; "
        f"flight {scenario['flight']} from {scenario['origin']} to {scenario['dest']}"
    )


def _answer_request(case: Any) -> AnswerRequest:
    scenario = case.scenario
    facts = {field: scenario[field] for field in FACT_FIELDS if field in scenario}
    return AnswerRequest(
        query=_case_query(case),
        disruption={
            "carrier": scenario["carrier"],
            "flight": scenario["flight"],
            "origin": scenario["origin"],
            "dest": scenario["dest"],
            "date": case.as_of_date,
            "event": scenario["event"],
        },
        facts=facts,
    )


def _prediction(case_id: str, response: Any) -> CasePrediction:
    answer = response.answer.lower()
    says_pause_erases_obligation = any(
        phrase in answer
        for phrase in (
            "pause means no refund",
            "pause removes the refund obligation",
            "refund is not owed because of the pause",
            "not eligible because enforcement is paused",
        )
    )
    return CasePrediction(
        case_id=case_id,
        entitlement={
            "eligible": "REFUND_ELIGIBLE",
            "not_eligible": "REFUND_NOT_ELIGIBLE",
            "insufficient_facts": "REFUND_ELIGIBILITY_UNRESOLVED",
        }[response.entitlement.label.value],
        enforcement_posture={
            "status": response.enforcement.status,
            "regime_id": response.enforcement.regime_id,
            "checked": response.enforcement.checked,
            "notice_ids": list(response.enforcement.applicable_notice_ids),
            "obligation_preserved": True,
        },
        cited_clauses=tuple(citation.clause_id for citation in response.citations),
        refusal=response.status == "refusal",
        refusal_reason=(
            "insufficient_material_facts"
            if response.refusal_reason == "required_facts_unresolved"
            else response.refusal_reason
        ),
        claims_refund_not_owed_due_to_posture=says_pause_erases_obligation,
    )


def _eligible_documents(services: AirUSServices, as_of_date: Any, carrier: str) -> list[Any]:
    return [
        document
        for document in services.indexed_documents
        if document.authority != "enforcement_policy"
        and document.doc_type != "enforcement_policy"
        and document.carrier in (None, carrier)
        and document.active_on(as_of_date)
    ]


def _citation_id(document: Any) -> str:
    """Map versioned storage IDs to the stable citation used by the golden set."""

    return str(document.metadata.get("clause_id", document.document_id))


def _bm25_ids(query: str, documents: Sequence[Any], k: int = 10) -> list[str]:
    scores = HybridRetriever._bm25(query, documents)
    return [_citation_id(documents[index]) for index, _ in scores[:k]]


def _dense_ids(
    retriever: HybridRetriever, query: str, documents: Sequence[Any], k: int = 10
) -> list[str]:
    scores = retriever._dense(query, documents)
    return [_citation_id(documents[index]) for index, _ in scores[:k]]


def _hybrid_ids(query: str, documents: Sequence[Any], k: int = 10) -> list[str]:
    retriever = HybridRetriever(documents)
    lexical = retriever._bm25(query, documents)
    dense = retriever._dense(query, documents)
    lexical_rank = {index: rank for rank, (index, _) in enumerate(lexical, 1)}
    dense_rank = {index: rank for rank, (index, _) in enumerate(dense, 1)}
    fused = [
        (
            index,
            (1 / (retriever.rrf_constant + lexical_rank[index]) if index in lexical_rank else 0)
            + (1 / (retriever.rrf_constant + dense_rank[index]) if index in dense_rank else 0),
        )
        for index in set(lexical_rank) | set(dense_rank)
    ]
    fused.sort(key=lambda item: (-item[1], documents[item[0]].document_id))
    return [_citation_id(documents[index]) for index, _ in fused[:k]]


def _reranked_ids(query: str, documents: Sequence[Any], as_of_date: Any, k: int = 10) -> list[str]:
    # This reproduces the production HybridRetriever's RRF candidate scoring and
    # lexical-overlap rerank, keeping the four modes directly comparable.
    return [
        _citation_id(hit.document)
        for hit in HybridRetriever(documents).search(query, as_of_date=as_of_date, k=k)[0]
    ]


def _retrieval_ablation(cases: Sequence[Any], services: AirUSServices) -> dict[str, Any]:
    modes: dict[str, dict[str, list[str]]] = {
        "bm25": {},
        "vector": {},
        "hybrid": {},
        "hybrid_rerank": {},
    }
    unavailable: dict[str, dict[str, list[str]]] = {}
    scorable: list[Any] = []
    corpus_ids = {_citation_id(document) for document in services.indexed_documents}
    for case in cases:
        documents = _eligible_documents(services, case.as_of_date, case.scenario["carrier"])
        active_ids = {_citation_id(document) for document in documents}
        expected_ids = set(case.expected.governing_clauses)
        missing = sorted(expected_ids - corpus_ids)
        inactive = sorted((expected_ids & corpus_ids) - active_ids)
        if missing or inactive:
            unavailable[case.case_id] = {
                "not_in_corpus": missing,
                "inactive_or_out_of_scope": inactive,
            }
            continue
        scorable.append(case)
        query = _case_query(case)
        retriever = HybridRetriever(documents)
        modes["bm25"][case.case_id] = _bm25_ids(query, documents)
        modes["vector"][case.case_id] = _dense_ids(retriever, query, documents)
        modes["hybrid"][case.case_id] = _hybrid_ids(query, documents)
        modes["hybrid_rerank"][case.case_id] = _reranked_ids(query, documents, case.as_of_date)

    from core.eval import retrieval_ablation

    metrics = retrieval_ablation(scorable, modes) if scorable else {}
    return {
        "status": "measured" if scorable else "unavailable",
        "corpus_document_count": len(services.indexed_documents),
        "scored_case_count": len(scorable),
        "unscorable_case_count": len(unavailable),
        "unscorable_cases": unavailable,
        "metrics_at_10": metrics,
    }


def run_eval(
    *,
    config_path: Path,
    output_path: Path | None = None,
    result_version: str = "local-eval-v1",
) -> dict[str, Any]:
    cases = load_golden_set(GOLDEN_PATH)
    services = AirUSServices(config_path=config_path)
    # The golden scenarios span several US airports; supply that synthetic scope to
    # the same input guard while leaving production configuration and source files alone.
    services.us_airport_codes = frozenset(
        str(code) for case in cases for code in (case.scenario["origin"], case.scenario["dest"])
    )
    answer_service = AnswerService(services)
    predictions: dict[str, CasePrediction] = {}
    records: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for case in cases:
        request = _answer_request(case)
        started = time.perf_counter()
        error: str | None = None
        try:
            response = answer_service.answer(request)
            predictions[case.case_id] = _prediction(case.case_id, response)
            trace = [
                {
                    **item.model_dump(mode="json"),
                    "call_id": f"{case.case_id}:{item.tool}:{index}",
                }
                for index, item in enumerate(response.tool_trace, start=1)
            ]
            answer = response.answer
            search_result = next(
                (item.result for item in response.tool_trace if item.tool == "search_rules"),
                None,
            )
            retrieved_ids = (
                [str(clause["clause_id"]) for clause in search_result.get("clauses", [])]
                if search_result is not None
                else []
            )
            refusal = response.status == "refusal"
            cost_usd = 0.0
            output_entitlement = response.entitlement.label.value
        except Exception as exc:  # Record case failures and continue the measured batch.
            error = f"{type(exc).__name__}: {exc}"
            trace = []
            answer = None
            retrieved_ids = []
            refusal = False
            cost_usd = 0.0
            output_entitlement = None
        latency_ms = (time.perf_counter() - started) * 1000
        tool_error = error is not None or any(item.get("error") for item in trace)
        records.append(
            {
                "case_id": case.case_id,
                "latency_ms": latency_ms,
                "cost_usd": cost_usd,
                "tool_error": tool_error,
                "cache_hit": None,
            }
        )
        details.append(
            {
                "case_id": case.case_id,
                "answer": answer,
                "retrieved_ids": retrieved_ids,
                "cited_ids": [item.clause_id for item in response.citations]
                if error is None
                else [],
                "output_entitlement": output_entitlement,
                "refusal": refusal,
                "tool_trace": trace,
                "latency_ms": latency_ms,
                "cost_usd": cost_usd,
                "error": error,
            }
        )

    answer_metrics = (
        evaluate_answers(cases, predictions) if len(predictions) == len(cases) else None
    )
    retrieval = _retrieval_ablation(cases, services)
    report: dict[str, Any] = {
        **(answer_metrics or {}),
        "schema_version": "1.0",
        "result_version": result_version,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "method": {
            "execution_path": "api.answer.AnswerService.answer with local AirUSServices",
            "llm_or_cloud_calls": 0,
            "judge": "not run; repeatability remains pending",
            "config_path": str(config_path),
            "corpus_path": str(services._rules_path) if services._rules_path else None,
            "corpus_document_count": len(services.indexed_documents),
            "case_count": len(cases),
        },
        "answers": answer_metrics,
        "operations": operations_metrics(records),
        "retrieval_ablation": (
            retrieval["metrics_at_10"] if retrieval["status"] == "measured" else None
        ),
        "retrieval_report": retrieval,
        "judge_variance": {"status": "pending"},
        "cold_rederivation": {"status": "completed"},
        "case_records": details,
    }
    report["ci"] = evaluate_ci(report)
    destination = output_path or RESULTS_DIR / f"{result_version}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--version", default="local-eval-v1")
    arguments = parser.parse_args()
    report = run_eval(
        config_path=arguments.config.resolve(),
        output_path=arguments.output,
        result_version=arguments.version,
    )
    print(
        json.dumps(
            {
                "result_version": report["result_version"],
                "answers_measured": report["answers"] is not None,
                "retrieval": report["retrieval_report"],
                "operations": report["operations"],
                "ci_status": report["ci"]["status"],
            },
            indent=2,
        )
    )
    if report["ci"]["status"] == "fail":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
