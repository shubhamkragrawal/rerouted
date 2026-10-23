"""Domain-neutral resolver value objects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core.authority import Authority


class EntitlementStatus(StrEnum):
    ELIGIBLE = "REFUND_ELIGIBLE"
    NOT_ELIGIBLE = "REFUND_NOT_ELIGIBLE"
    UNRESOLVED = "REFUND_ELIGIBILITY_UNRESOLVED"


class EnforcementStatus(StrEnum):
    ACTIVE = "enforcement_active"
    NO_PUBLISHED_CARVEOUT = "no_published_renumbering_carveout"
    DISCRETION = "enforcement_discretion"
    UNRESOLVED = "enforcement_unresolved"
    NOT_CHECKED = "enforcement_not_checked"


@dataclass(frozen=True)
class RuleCandidate:
    """A rule outcome before authority conflicts are resolved."""

    rule_id: str
    authority: Authority
    outcome: str
    rationale: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntitlementDecision:
    status: EntitlementStatus
    governing_clause_ids: tuple[str, ...]
    rationale_facts: tuple[str, ...]
    unresolved_facts: tuple[str, ...] = ()
    authority: Authority = Authority.REGULATION


@dataclass(frozen=True)
class EnforcementAssessment:
    """An enforcement answer independent of an entitlement answer.

    ``checked`` is deliberately separate from ``applicable_notice_ids``.  A
    checked lookup with no notices is evidence of a gap/default regime; an
    unchecked lookup means the system has no answer.
    """

    status: EnforcementStatus
    regime_id: str
    checked: bool
    applicable_notice_ids: tuple[str, ...] = ()
    rationale: str = ""
    provisional: bool = False
    verification_note: str | None = None
    unresolved_facts: tuple[str, ...] = ()
