"""Deterministic rule-engine primitives.

Domain packages provide predicates.  This package provides the small, auditable
types used to combine their results without asking a language model to decide.
"""

from core.authority import Authority

from .models import (
    EnforcementAssessment,
    EnforcementStatus,
    EntitlementDecision,
    EntitlementStatus,
    RuleCandidate,
)
from .precedence import AuthorityConflict, choose_authoritative_rule, precedence_rank

__all__ = [
    "Authority",
    "AuthorityConflict",
    "EntitlementDecision",
    "EntitlementStatus",
    "EnforcementAssessment",
    "EnforcementStatus",
    "RuleCandidate",
    "choose_authoritative_rule",
    "precedence_rank",
]
