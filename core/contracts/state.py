"""Shared state passed through the bounded Rerouted agent loop."""

from datetime import date
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field

from core.authority import Authority
from core.contracts.base import ContractModel
from core.contracts.tools import AlternativeFlightOption


class Disruption(ContractModel):
    carrier: Annotated[str, Field(min_length=2, max_length=3, pattern=r"^[A-Z0-9]+$")]
    flight: Annotated[str, Field(min_length=1, max_length=8)]
    date: date
    origin: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    dest: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    event: Annotated[str, Field(min_length=1)]


class ToolCall(ContractModel):
    tool: Annotated[str, Field(min_length=1)]
    arguments: dict[str, Any]
    call_id: Annotated[str, Field(min_length=1)]


class ToolResult(ContractModel):
    tool: Annotated[str, Field(min_length=1)]
    call_id: Annotated[str, Field(min_length=1)]
    result: dict[str, Any] | None = None
    error: str | None = None


class Citation(ContractModel):
    clause_id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    source_url: Annotated[str, Field(pattern=r"^https://")]
    authority: Authority
    effective_from: date
    effective_to: date | None = None


class EntitlementLabel(StrEnum):
    ELIGIBLE = "eligible"
    NOT_ELIGIBLE = "not_eligible"
    CONDITIONAL = "conditional"
    INSUFFICIENT_FACTS = "insufficient_facts"


class EnforcementQualifier(StrEnum):
    ENFORCED = "enforced"
    ENFORCEMENT_PAUSED = "enforcement_paused"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class Entitlement(ContractModel):
    label: EntitlementLabel
    enforcement: EnforcementQualifier
    clause_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    rationale: Annotated[str, Field(min_length=1)]


class RerouteState(ContractModel):
    """Serializable state with the hard safety budgets represented explicitly."""

    query: Annotated[str, Field(min_length=1)]
    disruption: Disruption
    as_of_date: date
    plan: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    options: tuple[AlternativeFlightOption, ...] = ()
    entitlement: Entitlement | None = None
    citations: tuple[Citation, ...] = ()
    unresolved_facts: tuple[str, ...] = ()
    llm_call_count: Annotated[int, Field(ge=0, le=8)] = 0
    cost_so_far_usd: Annotated[float, Field(ge=0, le=0.12)] = 0.0
    started_at: Annotated[float, Field(gt=0)]
    answer: str = ""
    refusal_reason: str | None = None
    trace_id: Annotated[str, Field(min_length=1)]
