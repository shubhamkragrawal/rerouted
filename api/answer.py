"""Deterministic, guarded answer composition over the four local tools."""

from __future__ import annotations

import time
from datetime import date
from typing import Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from api.telemetry import record_event
from core.agents import ToolExecutor, ToolSpec
from core.contracts.base import ContractModel
from core.contracts.state import (
    Citation,
    Disruption,
    EnforcementQualifier,
    Entitlement,
    EntitlementLabel,
    ToolCall,
    ToolResult,
)
from core.contracts.tools import (
    AlternativeFlightOption,
    DelayRiskRequest,
    DelayRiskResponse,
    FindAlternativesRequest,
    FindAlternativesResponse,
    SearchRulesRequest,
    SearchRulesResponse,
    WeatherContextRequest,
    WeatherContextResponse,
)
from core.guardrails import (
    DISCLOSURE,
    BudgetExceeded,
    BudgetLedger,
    BudgetLimits,
    Claim,
    GroundingAuditor,
    InputGuard,
    OutputGuard,
    build_refusal,
)
from core.resolver.models import EnforcementAssessment, EnforcementStatus
from core.retrieval.models import IndexedDocument
from domains.air_us.bootstrap import AirUSServices
from domains.air_us.rules.entitlement import (
    ConsumerDisposition,
    DisruptionFacts,
    ItineraryKind,
    resolve_air_travel,
)

MAX_EXPLICIT_LOCAL_TOOL_CALLS = 4
MAX_LOCAL_TOOL_CALLS = MAX_EXPLICIT_LOCAL_TOOL_CALLS + 1
ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


def _is_active_on(effective_from: date, effective_to: date | None, as_of_date: date) -> bool:
    return effective_from <= as_of_date and (
        effective_to is None or as_of_date <= effective_to
    )


class RefundFactsInput(ContractModel):
    """Facts for the deterministic refund predicate; omitted facts stay unknown."""

    covered_flight: bool | None = None
    merchant_of_record: bool | None = None
    itinerary_kind: ItineraryKind | None = None
    consumer_disposition: ConsumerDisposition = ConsumerDisposition.UNKNOWN
    cancelled: bool | None = None
    flight_number_changed: bool | None = None
    rebooked_on_renumbered_flight: bool | None = None
    departure_earlier_minutes: int | None = Field(default=None, ge=0)
    arrival_later_minutes: int | None = Field(default=None, ge=0)
    origin_or_destination_changed: bool | None = None
    added_connection: bool | None = None
    downgraded_class: bool | None = None
    passenger_has_disability: bool | None = None
    disability_connection_changed: bool | None = None
    needed_accessibility_feature_unavailable: bool | None = None


class AnswerRequest(ContractModel):
    query: str = Field(min_length=1, max_length=4000)
    disruption: Disruption
    facts: RefundFactsInput = Field(default_factory=RefundFactsInput)
    delay_request: DelayRiskRequest | None = None
    weather_request: WeatherContextRequest | None = None
    alternatives_request: FindAlternativesRequest | None = None

    @model_validator(mode="after")
    def nested_requests_match_disruption(self) -> AnswerRequest:
        disruption = self.disruption
        if self.delay_request and (
            self.delay_request.carrier != disruption.carrier
            or self.delay_request.origin != disruption.origin
            or self.delay_request.dest != disruption.dest
            or self.delay_request.flight_date != disruption.date
        ):
            raise ValueError("delay_request must describe the supplied disruption")
        if self.weather_request and self.weather_request.airport not in {
            disruption.origin,
            disruption.dest,
        }:
            raise ValueError("weather_request airport must be the disruption origin or destination")
        if self.alternatives_request and (
            self.alternatives_request.origin != disruption.origin
            or self.alternatives_request.dest != disruption.dest
            or self.alternatives_request.flight_date != disruption.date
        ):
            raise ValueError("alternatives_request must describe the supplied route and date")
        return self


class EnforcementView(ContractModel):
    status: str
    regime_id: str
    checked: bool
    applicable_notice_ids: tuple[str, ...]
    rationale: str
    unresolved_facts: tuple[str, ...]


class AnswerResponse(ContractModel):
    status: Literal["answer", "refusal"]
    answer: str
    refusal_reason: str | None
    unresolved_facts: tuple[str, ...]
    entitlement: Entitlement
    resolver_status: str
    enforcement: EnforcementView
    posture_checked: bool
    citations: tuple[Citation, ...]
    posture: tuple[dict[str, object], ...]
    delay_risk: DelayRiskResponse | None
    weather: WeatherContextResponse | None
    options: tuple[AlternativeFlightOption, ...]
    tool_trace: tuple[ToolResult, ...]
    disclosure: str = DISCLOSURE


class LocalToolPlanner:
    """Deterministic plan: always search rules, then only explicitly requested tools."""

    @staticmethod
    def plan(request: AnswerRequest) -> tuple[ToolCall, ...]:
        planned: list[ToolCall] = []
        rules_request = SearchRulesRequest(
            query=request.query,
            as_of_date=request.disruption.date,
            carrier=request.disruption.carrier,
            doc_types=(),
            k=10,
        )
        planned.append(
            ToolCall(
                tool="search_rules",
                arguments=rules_request.model_dump(mode="json"),
                call_id=str(uuid4()),
            )
        )
        for name, tool_request in (
            ("delay_risk", request.delay_request),
            ("weather_context", request.weather_request),
            ("find_alternatives", request.alternatives_request),
        ):
            if tool_request is not None:
                planned.append(
                    ToolCall(
                        tool=name,
                        arguments=tool_request.model_dump(mode="json"),
                        call_id=str(uuid4()),
                    )
                )
        if len(planned) > MAX_EXPLICIT_LOCAL_TOOL_CALLS:
            raise RuntimeError("local tool plan exceeded its fixed tool-call bound")
        return tuple(planned)


def _entitlement_label(value: str) -> EntitlementLabel:
    return {
        "REFUND_ELIGIBLE": EntitlementLabel.ELIGIBLE,
        "REFUND_NOT_ELIGIBLE": EntitlementLabel.NOT_ELIGIBLE,
        "REFUND_ELIGIBILITY_UNRESOLVED": EntitlementLabel.INSUFFICIENT_FACTS,
    }[value]


def _enforcement_qualifier(
    assessment: EnforcementAssessment, posture_checked: bool
) -> EnforcementQualifier:
    if (
        not posture_checked
        or not assessment.checked
        or assessment.status
        in {
            EnforcementStatus.UNRESOLVED,
            EnforcementStatus.NOT_CHECKED,
        }
    ):
        return EnforcementQualifier.UNKNOWN
    if assessment.status is EnforcementStatus.DISCRETION:
        return EnforcementQualifier.ENFORCEMENT_PAUSED
    return EnforcementQualifier.ENFORCED


class AnswerService:
    """Run guarded local tools, resolver, auditor, and refusal/output handling."""

    def __init__(self, services: AirUSServices) -> None:
        self.services = services
        self.input_guard = InputGuard(set(services.us_airport_codes))
        self.output_guard = OutputGuard()
        handlers = services.tool_handlers()
        self.executor = ToolExecutor(
            {
                "delay_risk": ToolSpec(
                    DelayRiskRequest, lambda request: handlers["delay_risk"](request)
                ),
                "search_rules": ToolSpec(
                    SearchRulesRequest, lambda request: handlers["search_rules"](request)
                ),
                "weather_context": ToolSpec(
                    WeatherContextRequest, lambda request: handlers["weather_context"](request)
                ),
                "find_alternatives": ToolSpec(
                    FindAlternativesRequest, lambda request: handlers["find_alternatives"](request)
                ),
            }
        )

    def answer(self, request: AnswerRequest) -> AnswerResponse:
        decision = self.input_guard.assess(
            request.query,
            origin=request.disruption.origin,
            destination=request.disruption.dest,
        )
        if not decision.allowed:
            return self._refused_without_tools(request, decision.reason or "input_rejected")

        calls = LocalToolPlanner.plan(request)
        budget = BudgetLedger(BudgetLimits(max_tool_calls=MAX_LOCAL_TOOL_CALLS))
        executed: list[ToolResult] = []
        budget_failure: str | None = None
        for call in calls:
            try:
                budget.reserve_tool_call()
                tool_started = time.perf_counter()
                executed.append(self.executor.execute(call))
                result = executed[-1]
                record_event(
                    "tool.completed",
                    tool=call.tool,
                    outcome="error" if result.error is not None else "success",
                    duration_ms=round((time.perf_counter() - tool_started) * 1000, 3),
                )
                budget.check_time()
            except BudgetExceeded as exc:
                budget_failure = exc.dimension
                executed.append(
                    ToolResult(
                        tool=call.tool,
                        call_id=call.call_id,
                        error=f"bounded_execution_stopped:{exc.dimension}",
                    )
                )
                break
        trace = tuple(executed)
        by_name = {item.tool: item for item in trace}
        search_result = by_name["search_rules"]
        rules = (
            SearchRulesResponse.model_validate(search_result.result)
            if search_result.result is not None
            else None
        )
        facts = request.facts
        resolved = resolve_air_travel(
            DisruptionFacts(
                as_of_date=request.disruption.date,
                covered_flight=facts.covered_flight,
                merchant_of_record=facts.merchant_of_record,
                itinerary_kind=facts.itinerary_kind,
                consumer_disposition=facts.consumer_disposition,
                cancelled=facts.cancelled,
                flight_number_changed=facts.flight_number_changed,
                rebooked_on_renumbered_flight=facts.rebooked_on_renumbered_flight,
                departure_earlier_minutes=facts.departure_earlier_minutes,
                arrival_later_minutes=facts.arrival_later_minutes,
                origin_or_destination_changed=facts.origin_or_destination_changed,
                added_connection=facts.added_connection,
                downgraded_class=facts.downgraded_class,
                passenger_has_disability=facts.passenger_has_disability,
                disability_connection_changed=facts.disability_connection_changed,
                needed_accessibility_feature_unavailable=facts.needed_accessibility_feature_unavailable,
            ),
            self.services.posture_registry,
        )
        required_clause_ids = set(resolved.entitlement.governing_clause_ids)
        first_clause_ids = (
            {
                clause.clause_id
                for clause in rules.clauses
                if _is_active_on(
                    clause.effective_from, clause.effective_to, request.disruption.date
                )
            }
            if rules is not None
            else set()
        )
        missing_clause_ids = required_clause_ids - first_clause_ids
        if missing_clause_ids and rules is not None and budget_failure is None:
            completion_call = ToolCall(
                tool="search_rules",
                arguments=SearchRulesRequest(
                    query=(
                        " governing clauses "
                        + " ".join(sorted(missing_clause_ids))
                    ),
                    as_of_date=request.disruption.date,
                    carrier=request.disruption.carrier,
                    doc_types=(),
                    k=10,
                ).model_dump(mode="json"),
                call_id=str(uuid4()),
            )
            try:
                budget.reserve_tool_call()
                tool_started = time.perf_counter()
                completion_result = self.executor.execute(completion_call)
                executed.append(completion_result)
                record_event(
                    "tool.completed",
                    tool=completion_call.tool,
                    outcome="error" if completion_result.error is not None else "success",
                    duration_ms=round((time.perf_counter() - tool_started) * 1000, 3),
                )
                budget.check_time()
                if completion_result.result is not None:
                    completion_rules = SearchRulesResponse.model_validate(
                        completion_result.result
                    )
                    active_completion_clauses = tuple(
                        clause
                        for clause in completion_rules.clauses
                        if _is_active_on(
                            clause.effective_from,
                            clause.effective_to,
                            request.disruption.date,
                        )
                    )
                    active_completion_posture = tuple(
                        notice
                        for notice in completion_rules.posture
                        if _is_active_on(
                            notice.effective_from,
                            notice.effective_to,
                            request.disruption.date,
                        )
                    )
                    combined_clauses = {
                        clause.clause_id: clause for clause in rules.clauses
                    }
                    combined_clauses.update(
                        {clause.clause_id: clause for clause in active_completion_clauses}
                    )
                    combined_posture = {notice.fr_doc: notice for notice in rules.posture}
                    combined_posture.update(
                        {notice.fr_doc: notice for notice in active_completion_posture}
                    )
                    rules = rules.model_copy(
                        update={
                            "clauses": tuple(combined_clauses.values()),
                            "posture": tuple(combined_posture.values()),
                            "posture_checked": rules.posture_checked
                            and completion_rules.posture_checked,
                        }
                    )
            except BudgetExceeded as exc:
                budget_failure = exc.dimension
                executed.append(
                    ToolResult(
                        tool=completion_call.tool,
                        call_id=completion_call.call_id,
                        error=f"bounded_execution_stopped:{exc.dimension}",
                    )
                )
        trace = tuple(executed)
        by_name = {item.tool: item for item in trace if item.tool != "search_rules"}
        by_name["search_rules"] = search_result
        posture_notice_ids = {item.fr_doc for item in rules.posture} if rules else set()
        posture_evidence_matches = bool(
            rules is not None
            and rules.posture_checked == resolved.enforcement.checked
            and posture_notice_ids == set(resolved.enforcement.applicable_notice_ids)
        )
        posture_checked = posture_evidence_matches and bool(rules and rules.posture_checked)
        enforcement_assessment = resolved.enforcement
        if not posture_evidence_matches:
            enforcement_assessment = EnforcementAssessment(
                status=EnforcementStatus.NOT_CHECKED,
                regime_id="posture_evidence_unavailable",
                checked=False,
                rationale=(
                    "The request's reviewed posture evidence did not match the "
                    "enforcement assessment."
                ),
                unresolved_facts=("posture_evidence_mismatch",),
            )
        enforcement_qualifier = _enforcement_qualifier(
            enforcement_assessment, posture_checked
        )
        resolver_entitlement = Entitlement(
            label=_entitlement_label(resolved.entitlement.status.value),
            enforcement=enforcement_qualifier,
            clause_ids=resolved.entitlement.governing_clause_ids,
            rationale="; ".join(resolved.entitlement.rationale_facts)
            or "required refund facts were not supplied",
        )
        enforcement_view = EnforcementView(
            status=enforcement_assessment.status.value,
            regime_id=enforcement_assessment.regime_id,
            checked=posture_checked and enforcement_assessment.checked,
            applicable_notice_ids=enforcement_assessment.applicable_notice_ids,
            rationale=enforcement_assessment.rationale,
            unresolved_facts=enforcement_assessment.unresolved_facts,
        )

        claims = tuple(
            Claim(
                text=f"Refund eligibility is {resolved.entitlement.status.value}.",
                clause_ids=(clause_id,),
            )
            for clause_id in resolved.entitlement.governing_clause_ids
        )
        request_evidence = tuple(
            IndexedDocument(
                document_id=clause.clause_id,
                text=clause.text,
                authority=clause.authority.value,
                effective_from=clause.effective_from,
                effective_to=clause.effective_to,
                source_url=clause.source_url,
                doc_type=clause.authority.value,
                metadata={"clause_id": clause.clause_id, "doc": clause.doc},
            )
            for clause in rules.clauses
        ) if rules is not None else ()
        audit = GroundingAuditor(request_evidence).audit(
            claims, as_of_date=request.disruption.date
        )
        audit_complete = (
            bool(claims) and not audit.must_refuse and len(audit.accepted_claims) == len(claims)
        )
        entitlement = resolver_entitlement
        if not audit_complete:
            entitlement = Entitlement(
                label=EntitlementLabel.INSUFFICIENT_FACTS,
                enforcement=enforcement_qualifier,
                clause_ids=tuple(citation.clause_id for citation in audit.citations),
                rationale="the required active-clause citation audit did not pass",
            )

        tool_errors = tuple(
            f"tool_execution_failed:{item.tool}" for item in trace if item.error is not None
        )
        unresolved = tuple(
            dict.fromkeys(
                resolved.entitlement.unresolved_facts
                + enforcement_assessment.unresolved_facts
                + tool_errors
                + ((f"budget_exceeded:{budget_failure}",) if budget_failure else ())
            )
        )
        reason: str | None = None
        if not audit_complete:
            reason = "grounding_audit_failed"
        elif budget_failure:
            reason = f"budget_exceeded:{budget_failure}"
        elif tool_errors:
            reason = "tool_execution_failed"
        elif unresolved:
            reason = "required_facts_unresolved"

        answer_text = self._compose(
            request=request,
            entitlement=entitlement,
            enforcement=enforcement_view,
            citations=audit.citations,
            rules=rules,
            reason=reason,
            unresolved=unresolved,
        )
        answer_text = self.output_guard.enforce(answer_text)
        return AnswerResponse(
            status="answer" if reason is None else "refusal",
            answer=answer_text,
            refusal_reason=reason,
            unresolved_facts=unresolved,
            entitlement=entitlement,
            resolver_status=resolved.entitlement.status.value,
            enforcement=enforcement_view,
            posture_checked=posture_checked,
            citations=audit.citations,
            posture=tuple(item.model_dump(mode="json") for item in rules.posture)
            if rules is not None
            else (),
            delay_risk=self._response_for(by_name.get("delay_risk"), DelayRiskResponse),
            weather=self._response_for(by_name.get("weather_context"), WeatherContextResponse),
            options=self._alternatives_for(by_name.get("find_alternatives")),
            tool_trace=trace,
        )

    def _response_for(
        self, result: ToolResult | None, response_model: type[ResponseModelT]
    ) -> ResponseModelT | None:
        if result is None or result.result is None:
            return None
        return response_model.model_validate(result.result)

    @staticmethod
    def _alternatives_for(result: ToolResult | None) -> tuple[AlternativeFlightOption, ...]:
        if result is None or result.result is None:
            return ()
        parsed = FindAlternativesResponse.model_validate(result.result)
        return parsed.options

    def _compose(
        self,
        *,
        request: AnswerRequest,
        entitlement: Entitlement,
        enforcement: EnforcementView,
        citations: tuple[Citation, ...],
        rules: SearchRulesResponse | None,
        reason: str | None,
        unresolved: tuple[str, ...],
    ) -> str:
        if reason is None:
            posture_text = (
                "The reviewed posture lookup reports an enforcement pause."
                if entitlement.enforcement is EnforcementQualifier.ENFORCEMENT_PAUSED
                else "The reviewed posture lookup found no applicable carve-out."
            )
            return (
                f"The deterministic refund resolver returned {entitlement.label.value} "
                f"for the supplied facts, based on {', '.join(entitlement.clause_ids)}. "
                f"{posture_text} {enforcement.regime_id} is the posture record for "
                f"{request.disruption.date.isoformat()}."
            )

        if citations:
            citation = citations[0]
            source_text = next(
                (
                    clause.text[:400]
                    for clause in (rules.clauses if rules is not None else ())
                    if clause.clause_id == citation.clause_id
                ),
                f"The active clause is {citation.clause_id}.",
            )
            missing = unresolved or (reason,)
            grounded = (
                f"The resolver returned {entitlement.label.value} from the supplied facts, "
                "but the combined answer remains incomplete."
            )
            return build_refusal(
                unresolved_facts=missing,
                grounded_partial=grounded,
                clause_id=citation.clause_id,
                clause_summary=source_text,
                conditional_fact=missing[0],
                conditional_value="verified",
            )
        return (
            "I can't answer this one reliably because no active, reviewed clause was "
            "available for the travel date. I am withholding a refund or enforcement "
            f"conclusion ({reason}); check the official source records before relying on one."
        )

    def _refused_without_tools(self, request: AnswerRequest, reason: str) -> AnswerResponse:
        state = Entitlement(
            label=EntitlementLabel.INSUFFICIENT_FACTS,
            enforcement=EnforcementQualifier.UNKNOWN,
            clause_ids=(),
            rationale="the input guard rejected the query before any source lookup",
        )
        text = self.output_guard.enforce(
            "I can't answer this reliably because the request did not pass the local "
            f"scope and privacy checks ({reason}). No source tools were called."
        )
        return AnswerResponse(
            status="refusal",
            answer=text,
            refusal_reason=reason,
            unresolved_facts=(),
            entitlement=state,
            resolver_status="REFUND_ELIGIBILITY_UNRESOLVED",
            enforcement=EnforcementView(
                status=EnforcementStatus.NOT_CHECKED.value,
                regime_id="not_checked",
                checked=False,
                applicable_notice_ids=(),
                rationale="The input guard stopped processing before posture lookup.",
                unresolved_facts=(),
            ),
            posture_checked=False,
            citations=(),
            posture=(),
            delay_risk=None,
            weather=None,
            options=(),
            tool_trace=(),
        )
