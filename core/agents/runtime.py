"""A small bounded loop with injectable planning and deterministic execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from core.contracts.state import RerouteState, ToolCall, ToolResult
from core.guardrails import (
    BudgetExceeded,
    BudgetLedger,
    Claim,
    GroundingAuditor,
    InputGuard,
    OutputGuard,
    OutputViolation,
    build_refusal,
)


@dataclass(frozen=True)
class ToolSpec:
    request_model: type[BaseModel]
    handler: Callable[[BaseModel], BaseModel | dict[str, Any]]


class ToolExecutor:
    def __init__(self, tools: dict[str, ToolSpec]) -> None:
        self._tools = dict(tools)

    def execute(self, call: ToolCall) -> ToolResult:
        spec = self._tools.get(call.tool)
        if spec is None:
            return ToolResult(tool=call.tool, call_id=call.call_id, error="unknown_tool")
        try:
            request = spec.request_model.model_validate(call.arguments)
            response = spec.handler(request)
            payload = (
                response.model_dump(mode="json")
                if isinstance(response, BaseModel)
                else response
            )
            return ToolResult(tool=call.tool, call_id=call.call_id, result=payload)
        except ValidationError as exc:
            return ToolResult(
                tool=call.tool,
                call_id=call.call_id,
                error=f"invalid_arguments: {exc.errors(include_url=False)}",
            )
        except Exception as exc:  # tool failures must become observations
            return ToolResult(tool=call.tool, call_id=call.call_id, error=f"tool_error: {exc}")


@dataclass(frozen=True)
class PlannerResult:
    tool_calls: tuple[ToolCall, ...] = ()
    final_answer: str | None = None
    claims: tuple[Claim, ...] = ()
    estimated_cost_usd: float = 0.0


Planner = Callable[[RerouteState], PlannerResult]


class AgentRuntime:
    """Run a planner/executor loop without allowing the planner to bypass budgets."""

    def __init__(
        self,
        executor: ToolExecutor,
        *,
        input_guard: InputGuard | None = None,
        auditor: GroundingAuditor | None = None,
        output_guard: OutputGuard | None = None,
    ) -> None:
        self._executor = executor
        self._input_guard = input_guard or InputGuard()
        self._auditor = auditor
        self._output_guard = output_guard or OutputGuard()

    def _refuse(
        self,
        state: RerouteState,
        reason: str,
        message: str,
    ) -> RerouteState:
        if state.unresolved_facts:
            clause_id = state.citations[0].clause_id if state.citations else "the cited rule"
            message = build_refusal(
                unresolved_facts=state.unresolved_facts,
                grounded_partial=message,
                clause_id=clause_id,
                clause_summary="the outcome depends on the missing fact",
                conditional_fact=state.unresolved_facts[0],
                conditional_value="confirmed",
            )
        return state.model_copy(
            update={
                "answer": self._output_guard.enforce(message),
                "refusal_reason": reason,
            }
        )

    def run(
        self,
        state: RerouteState,
        planner: Planner,
        *,
        ledger: BudgetLedger | None = None,
    ) -> RerouteState:
        decision = self._input_guard.assess(
            state.query,
            origin=state.disruption.origin,
            destination=state.disruption.dest,
        )
        if not decision.allowed:
            return self._refuse(
                state,
                decision.reason or "input_rejected",
                "I can only help with a US air-travel disruption after sensitive "
                "booking and contact details have been removed.",
            )

        active_ledger = ledger or BudgetLedger()
        current = state
        try:
            while True:
                active_ledger.reserve_llm_call()
                turn = planner(current)
                # Cost is checked separately after the planner reports it. Reserving
                # zero above guarantees the call-count limit cannot be bypassed.
                if turn.estimated_cost_usd:
                    projected = active_ledger.cost_usd + turn.estimated_cost_usd
                    if projected > active_ledger.limits.max_cost_usd:
                        raise BudgetExceeded("cost")
                    active_ledger.cost_usd = projected

                current = current.model_copy(
                    update={
                        "llm_call_count": active_ledger.llm_calls,
                        "cost_so_far_usd": active_ledger.cost_usd,
                    }
                )
                if turn.final_answer is not None:
                    citations = current.citations
                    if self._auditor is not None:
                        audit = self._auditor.audit(turn.claims, as_of_date=state.as_of_date)
                        if not turn.claims or audit.must_refuse:
                            return self._refuse(
                                current,
                                "grounding_audit_failed",
                                "I couldn't verify a load-bearing claim against a clause "
                                "that was in force on the disruption date.",
                            )
                        citations = audit.citations
                    try:
                        answer = self._output_guard.enforce(turn.final_answer)
                    except OutputViolation as exc:
                        return self._refuse(
                            current,
                            f"unsafe_output:{exc}",
                            "I can't reliably predict what the airline will actually do. "
                            "I can only explain the published rules and contracts.",
                        )
                    return current.model_copy(
                        update={"answer": answer, "citations": citations}
                    )
                if not turn.tool_calls:
                    return self._refuse(
                        current,
                        "planner_stalled",
                        "I couldn't gather enough grounded information to answer reliably.",
                    )
                results: list[ToolResult] = []
                for call in turn.tool_calls:
                    active_ledger.reserve_tool_call()
                    results.append(self._executor.execute(call))
                    active_ledger.check_time()
                current = current.model_copy(
                    update={
                        "plan": current.plan + turn.tool_calls,
                        "tool_results": current.tool_results + tuple(results),
                    }
                )
                active_ledger.check_time()
        except BudgetExceeded as exc:
            partial = self._output_guard.enforce(
                "I couldn't complete every check within the safety budget. "
                "Any tool results already collected remain available for review."
            )
            return current.model_copy(
                update={
                    "llm_call_count": active_ledger.llm_calls,
                    "cost_so_far_usd": min(
                        active_ledger.cost_usd, active_ledger.limits.max_cost_usd
                    ),
                    "answer": partial,
                    "refusal_reason": f"budget_exceeded:{exc.dimension}",
                }
            )
