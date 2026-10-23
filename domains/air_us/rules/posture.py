"""Date-bounded DOT enforcement-posture lookup."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from core.resolver.models import EnforcementAssessment, EnforcementStatus


@dataclass(frozen=True)
class PostureNotice:
    notice_id: str
    effect: str
    effective_from: date
    effective_to_exclusive: date
    sections: tuple[str, ...]
    scope: str
    source_url: str

    def in_force(self, as_of_date: date) -> bool:
        return self.effective_from <= as_of_date < self.effective_to_exclusive


@dataclass(frozen=True)
class PostureLookup:
    checked: bool
    as_of_date: date
    applicable_notices: tuple[PostureNotice, ...]


class PostureRegistry:
    def __init__(self, notices: tuple[PostureNotice, ...], metadata: dict[str, Any]):
        self.notices = notices
        self.metadata = metadata

    @classmethod
    def from_file(cls, path: Path) -> PostureRegistry:
        # posture.yaml intentionally uses the JSON subset of YAML.  This keeps
        # local mode dependency-free while remaining parseable by YAML tooling.
        payload = json.loads(path.read_text(encoding="utf-8"))
        notices = tuple(
            PostureNotice(
                notice_id=item["notice_id"],
                effect=item["effect"],
                effective_from=date.fromisoformat(item["effective_from"]),
                effective_to_exclusive=date.fromisoformat(
                    item["effective_to_exclusive"]
                ),
                sections=tuple(item["sections"]),
                scope=item["scope"],
                source_url=item["source_url"],
            )
            for item in payload["notices"]
        )
        return cls(notices=notices, metadata=payload.get("metadata", {}))

    def lookup(self, as_of_date: date) -> PostureLookup:
        refresh_from = self.metadata.get("refresh_required_from")
        if refresh_from and as_of_date >= date.fromisoformat(refresh_from):
            return PostureLookup(
                checked=False,
                as_of_date=as_of_date,
                applicable_notices=(),
            )
        return PostureLookup(
            checked=True,
            as_of_date=as_of_date,
            applicable_notices=tuple(
                notice for notice in self.notices if notice.in_force(as_of_date)
            ),
        )

    def assess(
        self,
        *,
        as_of_date: date,
        flight_renumbered: bool,
        rebooked_on_renumbered_flight: bool | None,
        significant_change: bool,
    ) -> EnforcementAssessment:
        lookup = self.lookup(as_of_date)
        if not lookup.checked:
            return EnforcementAssessment(
                status=EnforcementStatus.NOT_CHECKED,
                regime_id="refresh_required",
                checked=False,
                rationale=(
                    "The reviewed posture horizon has ended; later DOT action must "
                    "be checked before making an enforcement-posture claim."
                ),
            )
        if not lookup.applicable_notices:
            gap = self.metadata.get("verified_policy_gap", {})
            gap_start = date.fromisoformat(gap["effective_from"]) if gap else None
            gap_end = (
                date.fromisoformat(gap["effective_to_exclusive"]) if gap else None
            )
            is_policy_gap = bool(
                gap_start and gap_end and gap_start <= as_of_date < gap_end
            )
            regime_id = (
                "no_published_renumbering_carveout_july_gap"
                if is_policy_gap
                else "active_no_discretion_notice"
            )
            return EnforcementAssessment(
                status=EnforcementStatus.NO_PUBLISHED_CARVEOUT,
                regime_id=regime_id,
                checked=True,
                applicable_notice_ids=(),
                rationale=(
                    "No generally applicable renumbering-specific enforcement-"
                    "discretion notice is recorded as in force; this does not predict "
                    "whether DOT would bring a case."
                ),
                provisional=False,
                verification_note=gap.get("verification_note") if is_policy_gap else None,
            )

        notice_ids = tuple(notice.notice_id for notice in lookup.applicable_notices)
        if (
            flight_renumbered
            and not significant_change
            and rebooked_on_renumbered_flight is None
        ):
            return EnforcementAssessment(
                status=EnforcementStatus.UNRESOLVED,
                regime_id=f"notice_{notice_ids[0]}_scope_unresolved",
                checked=True,
                applicable_notice_ids=notice_ids,
                rationale="Whether the passenger was rebooked under the new number is unknown.",
                unresolved_facts=("rebooked_on_renumbered_flight",),
            )
        if (
            flight_renumbered
            and rebooked_on_renumbered_flight is True
            and not significant_change
        ):
            return EnforcementAssessment(
                status=EnforcementStatus.DISCRETION,
                regime_id=f"paused_{notice_ids[0]}",
                checked=True,
                applicable_notice_ids=notice_ids,
                rationale=(
                    "DOT notice covers a merely renumbered flight rebooked without "
                    "a defined significant change or delay."
                ),
            )
        return EnforcementAssessment(
            status=EnforcementStatus.ACTIVE,
            regime_id=f"notice_{notice_ids[0]}_scope_not_met",
            checked=True,
            applicable_notice_ids=notice_ids,
            rationale=(
                "An enforcement-discretion notice is in force, but its narrow "
                "renumbering-without-significant-change scope is not satisfied."
            ),
        )


def load_default_posture() -> PostureRegistry:
    return PostureRegistry.from_file(Path(__file__).parents[1] / "posture.yaml")
