"""Deterministic, useful refusal rendering."""

from __future__ import annotations

from collections.abc import Sequence


def build_refusal(
    *,
    unresolved_facts: Sequence[str],
    grounded_partial: str,
    clause_id: str,
    clause_summary: str,
    conditional_fact: str,
    conditional_value: str,
) -> str:
    missing = ", ".join(unresolved_facts) or "a required fact"
    return (
        "I can't answer this one reliably, because the rule that applies turns on "
        f"something I don't know: {missing}.\n\n"
        f"Here's what I can tell you: {grounded_partial}\n\n"
        f"The relevant rule is {clause_id} — {clause_summary}. "
        f"If {conditional_fact} turns out to be {conditional_value}, that clause would apply."
    )
