"""Focused contract checks for controller-boundary outcomes."""

import pytest
from pydantic import ValidationError

from local_app.outcomes import (
    OUTCOME_SCHEMA,
    BookingResult,
    ControllerOutcome,
    Intent,
    TransactionEvidence,
    TransactionStatus,
)


def test_outcome_has_versioned_schema_and_defaults_to_disclosure() -> None:
    outcome = ControllerOutcome(intent=Intent.ANSWER, deterministic_response="Rules summary")

    assert outcome.schema == OUTCOME_SCHEMA
    assert outcome.schema_version == 1
    assert outcome.simulator_disclosure_required is True
    assert outcome.authoritative_claims.claim_source == "deterministic_resolver"
    with pytest.raises(ValidationError):
        outcome.intent = Intent.REFUSE  # type: ignore[misc]


def test_simulated_booking_evidence_cannot_claim_real_side_effects() -> None:
    outcome = ControllerOutcome(
        intent=Intent.BOOK,
        booking_result=BookingResult(
            evidence=TransactionEvidence(
                status=TransactionStatus.SIMULATED,
                simulator="local_demo",
                simulated=True,
                reference="sim-123",
            ),
            itinerary_id="itinerary-1",
        ),
    )

    assert outcome.booking_result is not None
    assert outcome.booking_result.evidence.side_effect_performed is False
    assert outcome.simulator_disclosure_required is True


def test_transaction_evidence_rejects_inconsistent_simulation_and_intent() -> None:
    with pytest.raises(ValidationError):
        TransactionEvidence(
            status=TransactionStatus.SIMULATED,
            simulator="local_demo",
            simulated=False,
        )

    with pytest.raises(ValidationError):
        ControllerOutcome(intent=Intent.ANSWER, booking_result={})
