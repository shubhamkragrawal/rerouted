"""Immutable outcomes exchanged at the local controller boundary.

These contracts describe decisions and evidence only. They do not perform airline
transactions; hold and booking results are explicitly simulator evidence.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from core.contracts.base import ContractModel
from core.contracts.state import Entitlement
from core.contracts.tools import SearchRulesResponse

OUTCOME_SCHEMA = "rerouted.local_app.controller_outcome"
OUTCOME_SCHEMA_VERSION: Literal[1] = 1


class Intent(StrEnum):
    ANSWER = "answer"
    SEARCH_RULES = "search_rules"
    HOLD = "hold"
    BOOK = "book"
    REFUND = "refund"
    CANCEL = "cancel"
    CLARIFY = "clarify"
    REFUSE = "refuse"


class TransactionStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    SIMULATED = "simulated"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


class TransactionEvidence(ContractModel):
    """Evidence for a simulated operation, never a real airline-side transaction."""

    status: TransactionStatus
    simulator: Annotated[str, Field(min_length=1)]
    simulated: bool
    side_effect_performed: Literal[False] = False
    reference: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def status_matches_simulation(self) -> "TransactionEvidence":
        if (self.status == TransactionStatus.SIMULATED) != self.simulated:
            raise ValueError("simulated must be true exactly when status is simulated")
        return self


class HoldResult(ContractModel):
    evidence: TransactionEvidence
    hold_duration_minutes: Annotated[int, Field(ge=0)] | None = None


class BookingResult(ContractModel):
    evidence: TransactionEvidence
    itinerary_id: str | None = None


class AuthoritativeClaims(ContractModel):
    """Deterministic resolver outputs; language models cannot author these fields."""

    entitlement: Entitlement | None = None
    governing_clause_ids: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    claim_source: Literal["deterministic_resolver"] = "deterministic_resolver"


class ControllerOutcome(ContractModel):
    schema: Literal["rerouted.local_app.controller_outcome"] = OUTCOME_SCHEMA  # type: ignore[assignment]
    schema_version: Literal[1] = OUTCOME_SCHEMA_VERSION
    intent: Intent
    deterministic_response: str | None = None
    search_results: SearchRulesResponse | None = None
    hold_result: HoldResult | None = None
    booking_result: BookingResult | None = None
    authoritative_claims: AuthoritativeClaims = Field(default_factory=AuthoritativeClaims)
    fallback_reason: str | None = None
    simulator_disclosure_required: bool = True

    @model_validator(mode="after")
    def transactions_require_matching_intent(self) -> "ControllerOutcome":
        if self.hold_result is not None and self.intent != Intent.HOLD:
            raise ValueError("hold_result requires hold intent")
        if self.booking_result is not None and self.intent != Intent.BOOK:
            raise ValueError("booking_result requires book intent")
        return self
