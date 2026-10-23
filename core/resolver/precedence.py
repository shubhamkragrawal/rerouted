"""Authority precedence for obligation rules.

Enforcement policy is intentionally outside the obligation hierarchy.  It can
qualify whether an agency will pursue an obligation, but cannot erase it.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.authority import Authority

from .models import RuleCandidate


class AuthorityConflict(ValueError):
    """Raised when candidates cannot safely be combined."""


_OBLIGATION_PRECEDENCE = {
    Authority.REGULATION: 0,
    Authority.CONTRACT: 1,
    Authority.COMMITMENT: 2,
}


def precedence_rank(authority: Authority) -> int:
    """Return a stable rank for an obligation authority (lower is stronger)."""

    if authority is Authority.ENFORCEMENT_POLICY:
        raise AuthorityConflict(
            "enforcement policy is posture, not an entitlement authority"
        )
    return _OBLIGATION_PRECEDENCE[authority]


def choose_authoritative_rule(candidates: Iterable[RuleCandidate]) -> RuleCandidate:
    """Choose the strongest direct rule in a conflict set.

    Callers should only pass rules answering the same entitlement question.
    Supplemental carrier promises are separate benefits, not conflicts, and
    should be resolved independently.
    """

    materialized = tuple(candidates)
    if not materialized:
        raise AuthorityConflict("at least one rule candidate is required")

    posture_rules = [
        candidate
        for candidate in materialized
        if candidate.authority is Authority.ENFORCEMENT_POLICY
    ]
    if posture_rules:
        ids = ", ".join(candidate.rule_id for candidate in posture_rules)
        raise AuthorityConflict(
            f"posture rules cannot decide entitlement; remove: {ids}"
        )

    ordered = sorted(materialized, key=lambda item: precedence_rank(item.authority))
    best_rank = precedence_rank(ordered[0].authority)
    strongest = [
        candidate
        for candidate in ordered
        if precedence_rank(candidate.authority) == best_rank
    ]
    outcomes = {candidate.outcome for candidate in strongest}
    if len(outcomes) > 1:
        ids = ", ".join(candidate.rule_id for candidate in strongest)
        raise AuthorityConflict(f"same-authority rules conflict: {ids}")
    return strongest[0]
