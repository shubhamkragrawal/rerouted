"""Offline contract and bounded-workflow checks for the local controller graph."""

import pytest
from pydantic import ValidationError

from core.contracts.state import ToolCall, ToolResult
from local_app.controller import (
    ActionKind,
    ControllerAction,
    ControllerState,
    GraphStatus,
    LocalControllerGraph,
)
from local_app.outcomes import AuthoritativeClaims, Intent


def state() -> ControllerState:
    return ControllerState(session_id="session-1", query="Can I get a refund?")


def no_tools(call: ToolCall) -> ToolResult:
    return ToolResult(tool=call.tool, call_id=call.call_id, error="not_configured")


def no_claims(_: ControllerState) -> AuthoritativeClaims:
    return AuthoritativeClaims()


def audit(_: ControllerState, __: str) -> bool:
    return True


def graph(
    *, max_steps: int = 12, max_tool_calls: int = 12, max_repeated_actions: int = 2
) -> LocalControllerGraph:
    return LocalControllerGraph(
        tool_handler=no_tools,
        resolver=no_claims,
        auditor=audit,
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        max_repeated_actions=max_repeated_actions,
    )


def test_controller_state_versions_facts_and_keeps_conflicts_unresolved() -> None:
    first = state().record_facts({"cancelled": True})
    assert first.facts[0].version == 1
    assert first.facts[0].turn == 1

    conflict = first.record_facts({"cancelled": False})
    assert conflict.facts[0].value is True
    assert conflict.unresolved_facts == ("cancelled",)
    assert conflict.conflicts[0].proposed.version == 2

    confirmed = conflict.confirm_fact("cancelled", False)
    assert confirmed.facts[0].value is False
    assert confirmed.facts[0].version == 2
    assert confirmed.unresolved_facts == ()
    assert confirmed.turn == 2

    later = confirmed.next_user_turn().record_facts({"merchant_of_record": True})
    assert later.facts[-1].turn == 3


def test_conflict_confirmation_must_choose_a_recorded_value() -> None:
    conflict = state().record_facts({"cancelled": True}).record_facts({"cancelled": False})
    with pytest.raises(ValueError, match="select one"):
        conflict.confirm_fact("cancelled", "maybe")


def test_fact_change_and_confirmed_contradiction_invalidate_derived_state() -> None:
    derived = state().model_copy(
        update={
            "observations": (ToolResult(tool="search_rules", call_id="1", result={"ok": True}),),
            "authoritative_claims": AuthoritativeClaims(governing_clause_ids=("14CFR260.6(a)",)),
            "audit_passed": True,
            "completed_subgoals": ("search_rules",),
        }
    )
    changed = derived.record_facts({"cancelled": True})
    assert changed.observations == ()
    assert changed.authoritative_claims == AuthoritativeClaims()
    assert changed.audit_passed is None
    assert changed.completed_subgoals == ()

    conflicting = changed.model_copy(update={"observations": derived.observations}).record_facts(
        {"cancelled": False}
    )
    assert conflicting.observations == ()
    confirmed = conflicting.model_copy(update={"observations": derived.observations}).confirm_fact(
        "cancelled", False
    )
    assert confirmed.observations == ()
    assert confirmed.authoritative_claims == AuthoritativeClaims()
    assert confirmed.audit_passed is None
    assert confirmed.completed_subgoals == ()


def test_clarification_is_one_focused_question_and_checkpointed() -> None:
    result = graph().run(
        state(),
        lambda _: ControllerAction(
            kind=ActionKind.CLARIFY,
            missing_fact="consumer_disposition",
            prompt="Did you accept the replacement flight or compensation?",
        ),
    )

    assert result.state.status == GraphStatus.WAITING_FOR_USER
    assert result.state.unresolved_facts == ("consumer_disposition",)
    assert result.outcome.intent == Intent.CLARIFY
    assert result.state.checkpoints[-1].node == "clarify"
    assert result.state.checkpoints[-1].unresolved_facts == ("consumer_disposition",)


def test_tool_results_are_checkpointed_before_controller_replans() -> None:
    observed: list[ToolResult] = []

    def tool_handler(call: ToolCall) -> ToolResult:
        result = ToolResult(tool=call.tool, call_id=call.call_id, result={"ok": True})
        observed.append(result)
        return result

    workflow = LocalControllerGraph(
        tool_handler=tool_handler, resolver=no_claims, auditor=audit
    )
    n = 0

    def controller(current: ControllerState) -> ControllerAction:
        nonlocal n
        n += 1
        if n == 1:
            return ControllerAction(
                kind=ActionKind.CALL_TOOLS,
                calls=(ToolCall(tool="search_rules", arguments={"query": "refund"}, call_id="1"),),
            )
        assert current.observations == tuple(observed)
        return ControllerAction(kind=ActionKind.ANSWER, response="I found the rule.")

    result = workflow.run(state(), controller)
    assert result.outcome.intent == Intent.ANSWER
    assert result.state.observations[0].result == {"ok": True}
    assert [item.node for item in result.state.checkpoints] == [
        "parse",
        "tool_result_batch",
        "citation_audit",
        "finish",
    ]


def test_tool_exception_becomes_an_observation_and_graph_can_replan() -> None:
    def raising_tool(_: ToolCall) -> ToolResult:
        raise TimeoutError("local tool timed out")

    workflow = LocalControllerGraph(
        tool_handler=raising_tool, resolver=no_claims, auditor=audit
    )
    count = 0

    def controller(current: ControllerState) -> ControllerAction:
        nonlocal count
        count += 1
        if count == 1:
            return ControllerAction(
                kind=ActionKind.CALL_TOOLS,
                calls=(ToolCall(tool="weather_context", arguments={}, call_id="1"),),
            )
        assert current.observations[0].error == "tool_error: TimeoutError: local tool timed out"
        return ControllerAction(kind=ActionKind.REFUSE, response="Weather data is unavailable.")

    result = workflow.run(state(), controller)
    assert result.state.observations[0].error == "tool_error: TimeoutError: local tool timed out"
    assert result.outcome.intent == Intent.REFUSE


def test_resolver_is_the_only_source_of_authoritative_claims() -> None:
    expected = AuthoritativeClaims(governing_clause_ids=("14CFR260.6(a)",))
    resolved: list[ControllerState] = []

    def resolver(current: ControllerState) -> AuthoritativeClaims:
        resolved.append(current)
        return expected

    workflow = LocalControllerGraph(tool_handler=no_tools, resolver=resolver, auditor=audit)
    count = 0

    def controller(_: ControllerState) -> ControllerAction:
        nonlocal count
        count += 1
        return (
            ControllerAction(kind=ActionKind.RESOLVE)
            if count == 1
            else ControllerAction(kind=ActionKind.ANSWER, response="Refund rules summary")
        )

    result = workflow.run(state(), controller)
    assert resolved
    assert result.outcome.authoritative_claims == expected
    assert result.state.checkpoints[-3].node == "resolve"


@pytest.mark.parametrize("kind", [ActionKind.HOLD, ActionKind.BOOK])
def test_transaction_intents_only_return_simulated_evidence(kind: ActionKind) -> None:
    result = graph().run(state(), lambda _: ControllerAction(kind=kind, reference="demo-1"))
    evidence = (
        result.outcome.hold_result.evidence
        if kind == ActionKind.HOLD and result.outcome.hold_result is not None
        else result.outcome.booking_result.evidence
        if result.outcome.booking_result is not None
        else None
    )
    assert evidence is not None
    assert evidence.simulated is True
    assert evidence.side_effect_performed is False
    assert "Local simulation only" in (evidence.detail or "")


def test_answer_requires_a_successful_citation_audit() -> None:
    workflow = LocalControllerGraph(
        tool_handler=no_tools,
        resolver=no_claims,
        auditor=lambda _state, _answer: False,
    )
    result = workflow.run(
        state(), lambda _: ControllerAction(kind=ActionKind.ANSWER, response="Unsupported")
    )
    assert result.state.status == GraphStatus.QUALIFIED_REFUSAL
    assert result.state.audit_passed is False
    assert result.state.checkpoints[-2].node == "citation_audit"
    assert result.outcome.intent == Intent.REFUSE
    assert result.outcome.fallback_reason == "grounding_audit_failed"


def test_tool_call_budget_terminates_graph() -> None:
    workflow = LocalControllerGraph(
        tool_handler=no_tools,
        resolver=no_claims,
        auditor=audit,
        max_tool_calls=1,
    )
    result = workflow.run(
        state(),
        lambda _: ControllerAction(
            kind=ActionKind.CALL_TOOLS,
            calls=(
                ToolCall(tool="a", arguments={}, call_id="1"),
                ToolCall(tool="b", arguments={}, call_id="2"),
            ),
        ),
    )
    assert result.state.status == GraphStatus.BUDGET_EXCEEDED
    assert result.outcome.fallback_reason == "budget_exceeded:tool_calls"
    assert result.state.checkpoints[-1].node == "budget_exceeded"


def test_repeated_action_and_step_limit_terminate() -> None:
    repeated = graph(max_repeated_actions=1).run(
        state(), lambda _: ControllerAction(kind=ActionKind.RESOLVE)
    )
    assert repeated.state.status == GraphStatus.SYSTEM_ERROR
    assert repeated.outcome.fallback_reason == "repeated_controller_action"

    stalled = graph(max_steps=2).run(
        state(),
        lambda _: ControllerAction(
            kind=ActionKind.CALL_TOOLS,
            calls=(ToolCall(tool="lookup", arguments={}, call_id="same"),),
        ),
    )
    assert stalled.state.status == GraphStatus.BUDGET_EXCEEDED
    assert stalled.outcome.fallback_reason == "budget_exceeded:graph_steps"


def test_wall_time_budget_is_checked_before_work_and_after_sync_callbacks() -> None:
    now = 0.0
    workflow = LocalControllerGraph(
        tool_handler=no_tools,
        resolver=no_claims,
        auditor=audit,
        max_wall_seconds=1.0,
        clock=lambda: now,
    )

    def slow_controller(_: ControllerState) -> ControllerAction:
        nonlocal now
        now = 1.1
        return ControllerAction(kind=ActionKind.ANSWER, response="Too late")

    result = workflow.run(state(), slow_controller)
    assert result.state.status == GraphStatus.BUDGET_EXCEEDED
    assert result.outcome.fallback_reason == "budget_exceeded:wall_clock"


def test_actions_require_typed_payloads() -> None:
    with pytest.raises(ValidationError):
        ControllerAction(kind=ActionKind.CLARIFY)
    with pytest.raises(ValidationError):
        ControllerAction(kind=ActionKind.CALL_TOOLS)
