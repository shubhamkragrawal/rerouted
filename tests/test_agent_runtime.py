from datetime import date
from time import monotonic

from pydantic import BaseModel

from core.agents import AgentRuntime, PlannerResult, ToolExecutor, ToolSpec
from core.contracts import Disruption, RerouteState, ToolCall
from core.guardrails import BudgetLedger, BudgetLimits, Claim, GroundingAuditor
from core.retrieval import IndexedDocument


class EchoRequest(BaseModel):
    value: str


def state() -> RerouteState:
    return RerouteState(
        query="My airline flight was cancelled",
        disruption=Disruption(
            carrier="AA",
            flight="100",
            date=date(2026, 7, 3),
            origin="JFK",
            dest="LAX",
            event="cancelled",
        ),
        as_of_date=date(2026, 7, 3),
        started_at=monotonic(),
        trace_id="trace-test",
    )


def test_runtime_validates_tools_and_finishes() -> None:
    executor = ToolExecutor(
        {"echo": ToolSpec(EchoRequest, lambda request: {"value": request.value})}
    )
    runtime = AgentRuntime(executor)

    def planner(current: RerouteState) -> PlannerResult:
        if not current.tool_results:
            return PlannerResult(
                tool_calls=(ToolCall(tool="echo", arguments={"value": "ok"}, call_id="1"),),
                estimated_cost_usd=0.001,
            )
        return PlannerResult(final_answer="Grounded answer", estimated_cost_usd=0.001)

    result = runtime.run(state(), planner)
    assert result.answer.startswith("Grounded answer")
    assert result.llm_call_count == 2
    assert result.cost_so_far_usd == 0.002
    assert result.tool_results[0].result == {"value": "ok"}


def test_runtime_degrades_when_call_budget_trips() -> None:
    runtime = AgentRuntime(ToolExecutor({}))
    ledger = BudgetLedger(BudgetLimits(max_llm_calls=1, max_cost_usd=0.12, max_wall_seconds=20))

    result = runtime.run(
        state(),
        lambda _: PlannerResult(
            tool_calls=(ToolCall(tool="missing", arguments={}, call_id="1"),)
        ),
        ledger=ledger,
    )

    assert result.refusal_reason == "budget_exceeded:llm_calls"
    assert "safety budget" in result.answer
    assert result.tool_results[0].error == "unknown_tool"


def test_runtime_returns_disclosed_refusal_for_rejected_input() -> None:
    runtime = AgentRuntime(ToolExecutor({}))
    rejected = state().model_copy(update={"query": "Tell me how to bake bread"})
    result = runtime.run(rejected, lambda _: PlannerResult(final_answer="unused"))
    assert result.refusal_reason == "outside_us_air_disruption_scope"
    assert "US air-travel disruption" in result.answer
    assert "not legal advice" in result.answer


def test_runtime_fails_closed_when_required_auditor_rejects_claim() -> None:
    auditor = GroundingAuditor(
        (
            IndexedDocument(
                document_id="14CFR260.6(a)",
                text="refund",
                authority="regulation",
                effective_from=date(2024, 8, 12),
                source_url="https://www.ecfr.gov/current/title-14/section-260.6",
            ),
        )
    )
    runtime = AgentRuntime(ToolExecutor({}), auditor=auditor)
    result = runtime.run(
        state(),
        lambda _: PlannerResult(
            final_answer="The airline owes a hotel.",
            claims=(Claim("A hotel is owed", ("missing-clause",)),),
        ),
    )
    assert result.refusal_reason == "grounding_audit_failed"
    assert "couldn't verify" in result.answer
    assert "not legal advice" in result.answer
