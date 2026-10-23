"""A small, provider-free controller graph for local workflows and tests."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from time import monotonic
from typing import Annotated, Literal

from pydantic import Field, model_validator

from core.contracts.base import ContractModel
from core.contracts.state import ToolCall, ToolResult
from core.contracts.tools import SearchRulesResponse
from core.guardrails.budget import BudgetExceeded
from local_app.outcomes import (
    AuthoritativeClaims,
    BookingResult,
    ControllerOutcome,
    HoldResult,
    Intent,
    TransactionEvidence,
    TransactionStatus,
)

STATE_SCHEMA = "rerouted.local_app.controller_state"
STATE_SCHEMA_VERSION: Literal[1] = 1


class GraphStatus(StrEnum):
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    FINISHED = "finish"
    QUALIFIED_REFUSAL = "qualified_refusal"
    BUDGET_EXCEEDED = "budget_exceeded"
    SYSTEM_ERROR = "system_error"


class ActionKind(StrEnum):
    CALL_TOOLS = "call_tools"
    CLARIFY = "clarify"
    RESOLVE = "resolve"
    ANSWER = "answer"
    REFUSE = "refuse"
    HOLD = "hold"
    BOOK = "book"


FactValue = str | bool | int


class UserFact(ContractModel):
    key: Annotated[str, Field(min_length=1)]
    value: FactValue
    turn: Annotated[int, Field(ge=1)]
    version: Annotated[int, Field(ge=1)]


class FactConflict(ContractModel):
    key: Annotated[str, Field(min_length=1)]
    existing: UserFact
    proposed: UserFact


class Subgoal(ContractModel):
    key: Annotated[str, Field(min_length=1)]
    prerequisites: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    complete: bool = False


class ControllerCheckpoint(ContractModel):
    revision: Annotated[int, Field(ge=0)]
    node: Annotated[str, Field(min_length=1)]
    status: GraphStatus
    facts: tuple[UserFact, ...]
    unresolved_facts: tuple[str, ...]
    conflicts: tuple[FactConflict, ...]
    observations: tuple[ToolResult, ...]
    authoritative_claims: AuthoritativeClaims
    audit_passed: bool | None
    completed_subgoals: tuple[str, ...]


class ControllerState(ContractModel):
    """Serializable session memory; checkpoints contain validated state snapshots."""

    schema: Literal["rerouted.local_app.controller_state"] = STATE_SCHEMA  # type: ignore[assignment]
    schema_version: Literal[1] = STATE_SCHEMA_VERSION
    session_id: Annotated[str, Field(min_length=1)]
    query: Annotated[str, Field(min_length=1)]
    turn: Annotated[int, Field(ge=1)] = 1
    facts: tuple[UserFact, ...] = ()
    unresolved_facts: tuple[str, ...] = ()
    conflicts: tuple[FactConflict, ...] = ()
    subgoals: tuple[Subgoal, ...] = ()
    observations: tuple[ToolResult, ...] = ()
    authoritative_claims: AuthoritativeClaims = Field(default_factory=AuthoritativeClaims)
    audit_passed: bool | None = None
    completed_subgoals: tuple[str, ...] = ()
    checkpoints: tuple[ControllerCheckpoint, ...] = ()
    status: GraphStatus = GraphStatus.RUNNING
    transition_count: Annotated[int, Field(ge=0)] = 0
    refusal_reason: str | None = None

    def next_user_turn(self) -> ControllerState:
        """Start a follow-up turn while retaining validated session memory."""
        return self.model_copy(
            update={"turn": self.turn + 1, "status": GraphStatus.RUNNING, "refusal_reason": None}
        )

    def record_facts(self, values: dict[str, FactValue]) -> ControllerState:
        """Accept new facts; contradictory values remain unresolved until confirmed."""
        known = {fact.key: fact for fact in self.facts}
        conflicts = list(self.conflicts)
        accepted = dict(known)
        unresolved = list(self.unresolved_facts)
        changed = False
        for key, value in values.items():
            previous = known.get(key)
            if previous is not None and previous.value != value:
                changed = True
                proposed = UserFact(
                    key=key, value=value, turn=self.turn, version=previous.version + 1
                )
                conflicts = [conflict for conflict in conflicts if conflict.key != key]
                conflicts.append(FactConflict(key=key, existing=previous, proposed=proposed))
                if key not in unresolved:
                    unresolved.append(key)
            elif previous is None:
                changed = True
                accepted[key] = UserFact(key=key, value=value, turn=self.turn, version=1)
                unresolved = [item for item in unresolved if item != key]
        derived = (
            {
                "observations": (),
                "authoritative_claims": AuthoritativeClaims(),
                "audit_passed": None,
                "completed_subgoals": (),
            }
            if changed
            else {}
        )
        return self.model_copy(
            update={
                "facts": tuple(accepted.values()),
                "conflicts": tuple(conflicts),
                "unresolved_facts": tuple(unresolved),
                **derived,
            }
        )

    def confirm_fact(self, key: str, value: FactValue) -> ControllerState:
        conflict = next((item for item in self.conflicts if item.key == key), None)
        if conflict is None or value not in (conflict.existing.value, conflict.proposed.value):
            raise ValueError("confirmation must select one of the conflicting values")
        selected = conflict.proposed if value == conflict.proposed.value else conflict.existing
        facts = tuple(item for item in self.facts if item.key != key) + (selected,)
        return self.model_copy(
            update={
                "facts": facts,
                "conflicts": tuple(item for item in self.conflicts if item.key != key),
                "unresolved_facts": tuple(item for item in self.unresolved_facts if item != key),
                "turn": self.turn + 1,
                "observations": (),
                "authoritative_claims": AuthoritativeClaims(),
                "audit_passed": None,
                "completed_subgoals": (),
            }
        )


class ControllerAction(ContractModel):
    kind: ActionKind
    calls: tuple[ToolCall, ...] = ()
    missing_fact: str | None = None
    prompt: str | None = None
    response: str | None = None
    simulator: str = "local_demo"
    reference: str | None = None

    @model_validator(mode="after")
    def action_has_required_payload(self) -> ControllerAction:
        if self.kind == ActionKind.CALL_TOOLS and not self.calls:
            raise ValueError("call_tools requires at least one typed tool call")
        if self.kind == ActionKind.CLARIFY and (not self.missing_fact or not self.prompt):
            raise ValueError("clarify requires one missing_fact and a focused prompt")
        if self.kind in {ActionKind.ANSWER, ActionKind.REFUSE} and not self.response:
            raise ValueError("answer and refuse actions require response text")
        if self.kind != ActionKind.CALL_TOOLS and self.calls:
            raise ValueError("only call_tools actions may contain tool calls")
        return self


Controller = Callable[[ControllerState], ControllerAction]
ToolHandler = Callable[[ToolCall], ToolResult]
Resolver = Callable[[ControllerState], AuthoritativeClaims]
Auditor = Callable[[ControllerState, str], bool]


class ControllerRun(ContractModel):
    state: ControllerState
    outcome: ControllerOutcome


class LocalControllerGraph:
    """Bounded deterministic graph runner with injectable local fakes.

    Wall-time checks run before and after synchronous callbacks. Python cannot
    safely preempt an arbitrary blocking synchronous handler, so an overlong
    callback is detected and refused when it returns.
    """

    def __init__(
        self,
        *,
        tool_handler: ToolHandler,
        resolver: Resolver,
        auditor: Auditor | None = None,
        max_steps: int = 12,
        max_tool_calls: int = 12,
        max_repeated_actions: int = 2,
        max_wall_seconds: float = 20.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if min(max_steps, max_tool_calls, max_repeated_actions) < 1 or max_wall_seconds <= 0:
            raise ValueError("graph limits must be positive")
        self._tool_handler = tool_handler
        self._resolver = resolver
        self._auditor = auditor
        self._max_steps = max_steps
        self._max_tool_calls = max_tool_calls
        self._max_repeated_actions = max_repeated_actions
        self._max_wall_seconds = max_wall_seconds
        self._clock = clock

    def _check_deadline(self, started_at: float) -> None:
        if self._clock() - started_at >= self._max_wall_seconds:
            raise BudgetExceeded("wall_clock")

    @staticmethod
    def _checkpoint(state: ControllerState, node: str) -> ControllerState:
        checkpoint = ControllerCheckpoint(
            revision=len(state.checkpoints) + 1,
            node=node,
            status=state.status,
            facts=state.facts,
            unresolved_facts=state.unresolved_facts,
            conflicts=state.conflicts,
            observations=state.observations,
            authoritative_claims=state.authoritative_claims,
            audit_passed=state.audit_passed,
            completed_subgoals=state.completed_subgoals,
        )
        return state.model_copy(update={"checkpoints": state.checkpoints + (checkpoint,)})

    def run(self, state: ControllerState, controller: Controller) -> ControllerRun:
        started_at = self._clock()
        current = self._checkpoint(
            state.model_copy(update={"status": GraphStatus.RUNNING, "refusal_reason": None}),
            "parse",
        )
        tool_calls = 0
        seen: dict[str, int] = {}
        outcome: ControllerOutcome | None = None
        try:
            for _ in range(self._max_steps):
                self._check_deadline(started_at)
                current = current.model_copy(
                    update={"transition_count": current.transition_count + 1}
                )
                action = controller(current)
                self._check_deadline(started_at)
                signature = action.model_dump_json(exclude={"response", "prompt"})
                seen[signature] = seen.get(signature, 0) + 1
                if seen[signature] > self._max_repeated_actions:
                    raise RuntimeError("repeated_controller_action")

                if action.kind == ActionKind.CLARIFY:
                    fact = action.missing_fact
                    assert fact is not None and action.prompt is not None
                    unresolved = tuple(dict.fromkeys(current.unresolved_facts + (fact,)))
                    current = current.model_copy(
                        update={
                            "status": GraphStatus.WAITING_FOR_USER,
                            "unresolved_facts": unresolved,
                        }
                    )
                    current = self._checkpoint(current, "clarify")
                    outcome = ControllerOutcome(
                        intent=Intent.CLARIFY,
                        deterministic_response=action.prompt,
                        fallback_reason="awaiting_user_fact",
                    )
                    break

                if action.kind == ActionKind.CALL_TOOLS:
                    if tool_calls + len(action.calls) > self._max_tool_calls:
                        raise BudgetExceeded("tool_calls")
                    results_list: list[ToolResult] = []
                    for call in action.calls:
                        self._check_deadline(started_at)
                        results_list.append(self._execute_tool(call))
                        self._check_deadline(started_at)
                    results = tuple(results_list)
                    tool_calls += len(action.calls)
                    current = current.model_copy(
                        update={"observations": current.observations + results}
                    )
                    current = self._checkpoint(current, "tool_result_batch")
                    continue

                if action.kind == ActionKind.RESOLVE:
                    claims = self._resolver(current)
                    self._check_deadline(started_at)
                    current = current.model_copy(update={"authoritative_claims": claims})
                    current = self._checkpoint(current, "resolve")
                    continue

                if action.kind in {ActionKind.HOLD, ActionKind.BOOK}:
                    evidence = TransactionEvidence(
                        status=TransactionStatus.SIMULATED,
                        simulator=action.simulator,
                        simulated=True,
                        reference=action.reference,
                        detail="Local simulation only; no airline-side action was performed.",
                    )
                    if action.kind == ActionKind.HOLD:
                        outcome = ControllerOutcome(
                            intent=Intent.HOLD,
                            hold_result=HoldResult(evidence=evidence, hold_duration_minutes=0),
                        )
                    else:
                        outcome = ControllerOutcome(
                            intent=Intent.BOOK,
                            booking_result=BookingResult(evidence=evidence),
                        )
                    current = current.model_copy(update={"status": GraphStatus.FINISHED})
                    current = self._checkpoint(current, "finish")
                    break

                if action.kind == ActionKind.ANSWER:
                    assert action.response is not None
                    audit_passed = bool(
                        self._auditor is not None and self._auditor(current, action.response)
                    )
                    self._check_deadline(started_at)
                    current = current.model_copy(update={"audit_passed": audit_passed})
                    current = self._checkpoint(current, "citation_audit")
                    if not audit_passed:
                        current = current.model_copy(
                            update={
                                "status": GraphStatus.QUALIFIED_REFUSAL,
                                "refusal_reason": "grounding_audit_failed",
                            }
                        )
                        current = self._checkpoint(current, "qualified_refusal")
                        outcome = ControllerOutcome(
                            intent=Intent.REFUSE,
                            deterministic_response=(
                                "I couldn't verify the answer against the available local evidence."
                            ),
                            fallback_reason="grounding_audit_failed",
                            authoritative_claims=current.authoritative_claims,
                        )
                        break
                    current = current.model_copy(update={"status": GraphStatus.FINISHED})
                    current = self._checkpoint(current, "finish")
                    search_result = self._latest_search_result(current.observations)
                    outcome = ControllerOutcome(
                        intent=Intent.ANSWER,
                        deterministic_response=action.response,
                        search_results=search_result,
                        authoritative_claims=current.authoritative_claims,
                    )
                    break

                assert action.kind == ActionKind.REFUSE
                assert action.response is not None
                current = current.model_copy(
                    update={
                        "status": GraphStatus.QUALIFIED_REFUSAL,
                        "refusal_reason": "controller_refusal",
                    }
                )
                current = self._checkpoint(current, "qualified_refusal")
                outcome = ControllerOutcome(
                    intent=Intent.REFUSE,
                    deterministic_response=action.response,
                    fallback_reason="controller_refusal",
                    authoritative_claims=current.authoritative_claims,
                )
                break
            else:
                raise BudgetExceeded("graph_steps")
        except BudgetExceeded as exc:
            current = current.model_copy(
                update={
                    "status": GraphStatus.BUDGET_EXCEEDED,
                    "refusal_reason": f"budget_exceeded:{exc.dimension}",
                }
            )
            current = self._checkpoint(current, "budget_exceeded")
            outcome = ControllerOutcome(
                intent=Intent.REFUSE,
                deterministic_response=(
                    "I couldn't complete the request within the local controller budget."
                ),
                fallback_reason=f"budget_exceeded:{exc.dimension}",
                authoritative_claims=current.authoritative_claims,
            )
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            current = current.model_copy(
                update={"status": GraphStatus.SYSTEM_ERROR, "refusal_reason": reason}
            )
            current = self._checkpoint(current, "system_error")
            outcome = ControllerOutcome(
                intent=Intent.REFUSE,
                deterministic_response=(
                    "I couldn't complete the request safely using the local controller."
                ),
                fallback_reason=reason,
                authoritative_claims=current.authoritative_claims,
            )
        assert outcome is not None
        return ControllerRun(state=current, outcome=outcome)

    @staticmethod
    def _latest_search_result(observations: tuple[ToolResult, ...]) -> SearchRulesResponse | None:
        for observation in reversed(observations):
            if observation.tool == "search_rules" and observation.result is not None:
                try:
                    return SearchRulesResponse.model_validate(observation.result)
                except Exception:
                    return None
        return None

    def _execute_tool(self, call: ToolCall) -> ToolResult:
        try:
            return self._tool_handler(call)
        except Exception as exc:  # failures become observations for the next controller step
            return ToolResult(
                tool=call.tool,
                call_id=call.call_id,
                error=f"tool_error: {type(exc).__name__}: {exc}",
            )
