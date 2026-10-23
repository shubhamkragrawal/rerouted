from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from api.answer import MAX_LOCAL_TOOL_CALLS, AnswerRequest, LocalToolPlanner
from api.main import create_app
from core.contracts.state import Disruption
from core.contracts.tools import (
    DelayRiskRequest,
    FindAlternativesRequest,
    SearchRulesRequest,
    SearchRulesResponse,
    WeatherContextRequest,
    WeatherMode,
)
from domains.air_us.bootstrap import AirUSServices

FIXTURES = Path(__file__).parent / "fixtures"


def test_answer_planner_caps_the_plan_at_four_explicit_tool_calls() -> None:
    request = AnswerRequest(
        query="Was my cancelled flight refundable?",
        disruption=Disruption(
            carrier="AA",
            flight="100",
            date=date(2026, 3, 14),
            origin="JFK",
            dest="LAX",
            event="cancelled",
        ),
        delay_request=DelayRiskRequest(
            carrier="AA",
            origin="JFK",
            dest="LAX",
            flight_date=date(2026, 3, 14),
            crs_dep_time=datetime(2026, 3, 14, 8).time(),
            mode="scheduled",
        ),
        weather_request=WeatherContextRequest(
            airport="JFK",
            window_start=datetime(2026, 3, 14, 12, tzinfo=UTC),
            window_end=datetime(2026, 3, 14, 13, tzinfo=UTC),
            mode=WeatherMode.HISTORICAL,
        ),
        alternatives_request=FindAlternativesRequest(
            origin="JFK",
            dest="LAX",
            not_before=datetime(2026, 3, 14, 12, tzinfo=UTC),
            original_scheduled_arrival=datetime(2026, 3, 14, 18, tzinfo=UTC),
            max_stops=1,
            same_carrier_only=False,
            flight_date=date(2026, 3, 14),
        ),
    )
    plan = LocalToolPlanner.plan(request)
    assert len(plan) == 4
    assert {call.tool for call in plan} == {
        "search_rules",
        "delay_risk",
        "weather_context",
        "find_alternatives",
    }
    assert MAX_LOCAL_TOOL_CALLS == 5


def _local_services(root: Path) -> AirUSServices:
    (root / "data/rules").mkdir(parents=True)
    (root / "data/bts").mkdir(parents=True)
    (root / "data/ncei").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "bts/sample.csv", root / "data/bts/2026-03.csv")
    with (root / "data/bts/2026-03.csv").open("a", encoding="utf-8") as stream:
        stream.write("2026-03-14,AA,999,RIC,PHL,800,900,0,0\n")
    shutil.copyfile(FIXTURES / "ncei/sample.csv", root / "data/ncei/USW00094789.csv")
    documents = [
        ("14CFR260.2", "Refund for a covered flight cancellation under section 260.2."),
        ("14CFR260.6(a)(1)", "A passenger who accepts alternative travel is not due a refund."),
        ("14CFR260.6(a)(2)", "A passenger who rejects all alternatives may receive a refund."),
        ("14CFR260.3", "Part 260 applies to covered flights and the merchant of record."),
    ]
    with (root / "data/rules/documents.jsonl").open("w", encoding="utf-8") as stream:
        for clause_id, text in documents:
            stream.write(
                json.dumps(
                    {
                        "document_id": clause_id,
                        "text": text,
                        "authority": "regulation",
                        "effective_from": "2024-01-01",
                        "source_url": "https://www.ecfr.gov/current/title-14/section-260.6",
                        "doc_type": "regulation",
                        "metadata": {"doc": "14 CFR Part 260"},
                    }
                )
                + "\n"
            )
    airports = {
        "JFK": {"timezone": "America/New_York", "ncei_station": "USW00094789"},
        "ORD": {"timezone": "America/Chicago", "ncei_station": "USW00094846"},
        "LAX": {"timezone": "America/Los_Angeles", "ncei_station": "USW00023174"},
    }
    (root / "config/airports.yaml").write_text(yaml.safe_dump(airports), encoding="utf-8")
    config = {
        "paths": {
            "rules_documents": "data/rules/documents.jsonl",
            "delay_model": "data/model/not-installed.json",
            "bts_schedule_template": "data/bts/{year}-{month:02d}.csv",
            "ncei_station_template": "data/ncei/{station}.csv",
            "airport_metadata": "config/airports.yaml",
        },
        "data_version": {"rules": "integration-fixture-v1"},
        "airports": {},
        "nws": {"user_agent": "Rerouted/0.1 (contact: test@example.test)"},
    }
    config_path = root / "config/default.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return AirUSServices(config_path=config_path)


def test_rest_routes_use_configured_local_artifacts(tmp_path: Path) -> None:
    services = _local_services(tmp_path)
    client = TestClient(create_app(services))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["components"]["search_rules"]["ready"] is True
    assert health.json()["components"]["delay_risk"]["ready"] is False
    assert client.get("/ready").status_code == 503

    rules = client.post(
        "/rules/search",
        json={
            "query": "refund cancellation",
            "as_of_date": "2026-03-14",
            "doc_types": ["regulation"],
            "k": 10,
        },
    )
    assert rules.status_code == 200
    assert rules.json()["corpus_version"] == "integration-fixture-v1"
    assert rules.json()["posture_checked"] is True
    assert rules.json()["retrieval_provider"] == "hash"
    assert rules.json()["retrieval_model"] == "blake2b-token-hash-v1"
    assert rules.json()["retrieval_fallback_reason"] is None

    weather = client.post(
        "/weather",
        json={
            "airport": "JFK",
            "window_start": "2026-03-14T16:00:00Z",
            "window_end": "2026-03-14T17:00:00Z",
            "mode": "historical",
        },
    )
    assert weather.status_code == 200
    assert weather.json()["source"] == "NCEI-LCD"
    assert len(weather.json()["observations"]) == 1

    alternatives = client.post(
        "/alternatives",
        json={
            "origin": "JFK",
            "dest": "LAX",
            "not_before": "2026-03-14T12:00:00Z",
            "original_scheduled_arrival": "2026-03-14T18:00:00Z",
            "max_stops": 1,
            "same_carrier_only": False,
            "flight_date": "2026-03-14",
        },
    )
    assert alternatives.status_code == 200
    assert alternatives.json()["schedule_source"] == "historical_bts"
    assert alternatives.json()["options"]

    prediction = client.post(
        "/predict",
        json={
            "carrier": "AA",
            "origin": "JFK",
            "dest": "LAX",
            "flight_date": "2026-03-14",
            "crs_dep_time": "08:00:00",
            "mode": "scheduled",
        },
    )
    assert prediction.status_code == 503

    answer = client.post(
        "/answer",
        json={
            "query": "Was I entitled to a refund after my flight was cancelled?",
            "disruption": {
                "carrier": "AA",
                "flight": "100",
                "date": "2026-03-14",
                "origin": "JFK",
                "dest": "LAX",
                "event": "cancelled",
            },
            "facts": {
                "covered_flight": True,
                "merchant_of_record": True,
                "itinerary_kind": "domestic",
                "consumer_disposition": "rejected_all",
                "cancelled": True,
            },
            "weather_request": {
                "airport": "JFK",
                "window_start": "2026-03-14T16:00:00Z",
                "window_end": "2026-03-14T17:00:00Z",
                "mode": "historical",
            },
            "alternatives_request": {
                "origin": "JFK",
                "dest": "LAX",
                "not_before": "2026-03-14T12:00:00Z",
                "original_scheduled_arrival": "2026-03-14T18:00:00Z",
                "max_stops": 1,
                "same_carrier_only": False,
                "flight_date": "2026-03-14",
            },
        },
    )
    assert answer.status_code == 200
    result = answer.json()
    assert result["status"] == "answer"
    assert result["posture_checked"] is True
    assert result["citations"]
    assert {item["tool"] for item in result["tool_trace"]} == {
        "search_rules",
        "weather_context",
        "find_alternatives",
    }
    search_trace = next(item for item in result["tool_trace"] if item["tool"] == "search_rules")
    assert search_trace["result"]["retrieval_provider"] == "hash"
    assert search_trace["result"]["retrieval_model"] == "blake2b-token-hash-v1"
    assert result["disclosure"]

    out_of_scope = client.post(
        "/answer",
        json={
            "query": "What refund applies after my flight was cancelled?",
            "disruption": {
                "carrier": "AA",
                "flight": "100",
                "date": "2026-03-14",
                "origin": "SFO",
                "dest": "LAX",
                "event": "cancelled",
            },
        },
    )
    assert out_of_scope.status_code == 200
    assert out_of_scope.json()["status"] == "refusal"
    assert out_of_scope.json()["refusal_reason"] == "outside_us_airport_scope"
    assert out_of_scope.json()["tool_trace"] == []


def test_answer_fails_closed_when_request_search_returns_no_clauses(
    tmp_path: Path, monkeypatch: object
) -> None:
    services = _local_services(tmp_path)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        services,
        "search_rules",
        lambda request: SearchRulesResponse(
            clauses=(),
            posture=(),
            posture_checked=True,
            as_of_date=request.as_of_date,
            excluded_by_date=0,
            corpus_version="empty-search-fixture-v1",
        ),
    )
    client = TestClient(create_app(services))

    response = client.post(
        "/answer",
        json={
            "query": "Was my cancelled flight refundable?",
            "disruption": {
                "carrier": "AA",
                "flight": "100",
                "date": "2026-03-14",
                "origin": "JFK",
                "dest": "LAX",
                "event": "cancelled",
            },
            "facts": {
                "covered_flight": True,
                "merchant_of_record": True,
                "itinerary_kind": "domestic",
                "consumer_disposition": "rejected_all",
                "cancelled": True,
            },
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "refusal"
    assert result["refusal_reason"] == "grounding_audit_failed"
    assert result["citations"] == []
    assert result["entitlement"]["label"] == "insufficient_facts"


def test_answer_completes_missing_governing_clause_with_bounded_search(
    tmp_path: Path, monkeypatch: object
) -> None:
    services = _local_services(tmp_path)
    original_search_rules = services.search_rules
    calls: list[SearchRulesRequest] = []

    def search_with_missing_first_clause(request: SearchRulesRequest) -> SearchRulesResponse:
        calls.append(request)
        response = original_search_rules(request)
        if len(calls) == 1:
            return response.model_copy(
                update={
                    "clauses": tuple(
                        clause
                        for clause in response.clauses
                        if clause.clause_id != "14CFR260.6(a)(2)"
                    )
                }
            )
        return response

    monkeypatch.setattr(  # type: ignore[attr-defined]
        services, "search_rules", search_with_missing_first_clause
    )
    client = TestClient(create_app(services))

    response = client.post(
        "/answer",
        json={
            "query": "Was my cancelled flight refundable?",
            "disruption": {
                "carrier": "AA",
                "flight": "100",
                "date": "2026-03-14",
                "origin": "JFK",
                "dest": "LAX",
                "event": "cancelled",
            },
            "facts": {
                "covered_flight": True,
                "merchant_of_record": True,
                "itinerary_kind": "domestic",
                "consumer_disposition": "rejected_all",
                "cancelled": True,
            },
        },
    )

    result = response.json()
    assert result["status"] == "answer"
    assert len(calls) == 2
    assert "14CFR260.6(a)(2)" in calls[1].query
    assert [item["tool"] for item in result["tool_trace"]].count("search_rules") == 2
    assert {item["clause_id"] for item in result["citations"]} == {
        "14CFR260.2",
        "14CFR260.6(a)(1)",
        "14CFR260.6(a)(2)",
    }


def test_answer_refuses_when_clause_completion_search_is_empty(
    tmp_path: Path, monkeypatch: object
) -> None:
    services = _local_services(tmp_path)
    original_search_rules = services.search_rules
    calls = 0

    def empty_completion(request: SearchRulesRequest) -> SearchRulesResponse:
        nonlocal calls
        calls += 1
        response = original_search_rules(request)
        if calls == 1:
            return response.model_copy(
                update={
                    "clauses": tuple(
                        clause
                        for clause in response.clauses
                        if clause.clause_id != "14CFR260.6(a)(2)"
                    )
                }
            )
        return response.model_copy(update={"clauses": ()})

    monkeypatch.setattr(  # type: ignore[attr-defined]
        services, "search_rules", empty_completion
    )
    client = TestClient(create_app(services))

    response = client.post(
        "/answer",
        json={
            "query": "Was my cancelled flight refundable?",
            "disruption": {
                "carrier": "AA",
                "flight": "100",
                "date": "2026-03-14",
                "origin": "JFK",
                "dest": "LAX",
                "event": "cancelled",
            },
            "facts": {
                "covered_flight": True,
                "merchant_of_record": True,
                "itinerary_kind": "domestic",
                "consumer_disposition": "rejected_all",
                "cancelled": True,
            },
        },
    )

    result = response.json()
    assert result["status"] == "refusal"
    assert result["refusal_reason"] == "grounding_audit_failed"
    assert calls == 2
    assert "14CFR260.6(a)(2)" not in {
        item["clause_id"] for item in result["citations"]
    }


def test_answer_fails_closed_when_search_omits_resolver_enforcement_notice(
    tmp_path: Path, monkeypatch: object
) -> None:
    services = _local_services(tmp_path)
    original_search_rules = services.search_rules

    def search_without_posture(request: SearchRulesRequest) -> SearchRulesResponse:
        response = original_search_rules(request)
        return response.model_copy(update={"posture": ()})

    monkeypatch.setattr(  # type: ignore[attr-defined]
        services, "search_rules", search_without_posture
    )
    client = TestClient(create_app(services))

    response = client.post(
        "/answer",
        json={
            "query": "Does the renumbered flight enforcement pause apply?",
            "disruption": {
                "carrier": "AA",
                "flight": "100",
                "date": "2026-03-14",
                "origin": "JFK",
                "dest": "LAX",
                "event": "renumbered",
            },
            "facts": {
                "covered_flight": True,
                "merchant_of_record": True,
                "itinerary_kind": "domestic",
                "consumer_disposition": "rejected_all",
                "cancelled": False,
                "flight_number_changed": True,
                "rebooked_on_renumbered_flight": True,
                "departure_earlier_minutes": 0,
                "arrival_later_minutes": 0,
                "origin_or_destination_changed": False,
                "added_connection": False,
                "downgraded_class": False,
                "passenger_has_disability": False,
            },
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "refusal"
    assert result["refusal_reason"] == "required_facts_unresolved"
    assert result["posture_checked"] is False
    assert result["enforcement"]["status"] == "enforcement_not_checked"
    assert result["enforcement"]["applicable_notice_ids"] == []
    assert "posture_evidence_mismatch" in result["unresolved_facts"]
