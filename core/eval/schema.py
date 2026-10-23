"""Schema and count validation for the hand-authored golden set."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from core.contracts.base import ContractModel

GoldenFamily = Literal[
    "domestic_cancel_controllable",
    "domestic_cancel_weather",
    "domestic_delay_over_3h",
    "domestic_delay_under_3h",
    "international_delay_6h",
    "enforcement_posture",
    "downgrade_added_connection",
    "insufficient_facts",
]
EXPECTED_FAMILIES: tuple[GoldenFamily, ...] = (
    "domestic_cancel_controllable",
    "domestic_cancel_weather",
    "domestic_delay_over_3h",
    "domestic_delay_under_3h",
    "international_delay_6h",
    "enforcement_posture",
    "downgrade_added_connection",
    "insufficient_facts",
)
EntitlementLabel = Literal[
    "REFUND_ELIGIBLE", "REFUND_NOT_ELIGIBLE", "REFUND_ELIGIBILITY_UNRESOLVED"
]
PostureLabel = Literal[
    "enforcement_active",
    "no_published_renumbering_carveout",
    "enforcement_discretion",
    "enforcement_unresolved",
    "enforcement_not_checked",
]


class ExpectedPosture(ContractModel):
    status: PostureLabel
    regime_id: str = Field(min_length=1)
    checked: bool
    notice_ids: tuple[str, ...]
    obligation_preserved: bool = True


class GoldenExpected(ContractModel):
    entitlement: EntitlementLabel
    enforcement_posture: ExpectedPosture
    governing_clauses: tuple[str, ...] = Field(min_length=1)
    expected_refusal: bool
    refusal_reason: str | None = None


class GoldenCase(ContractModel):
    case_id: str = Field(pattern=r"^gs-\d{3}$")
    family: GoldenFamily
    scenario: dict[str, Any]
    as_of_date: date
    expected: GoldenExpected
    source_of_truth: str = Field(min_length=1)
    difficulty: Literal["easy", "medium", "hard"]

    @model_validator(mode="after")
    def scenario_has_stable_identity(self) -> GoldenCase:
        required = {"flight", "flight_date", "carrier", "origin", "dest", "event"}
        missing = required - set(self.scenario)
        if missing:
            raise ValueError(f"scenario is missing identity fields: {sorted(missing)}")
        if date.fromisoformat(str(self.scenario["flight_date"])) != self.as_of_date:
            raise ValueError("scenario.flight_date must equal as_of_date for reproducible replay")
        for field_name in ("origin", "dest"):
            value = self.scenario[field_name]
            if not isinstance(value, str) or re.fullmatch(r"[A-Z]{3}", value) is None:
                raise ValueError(
                    f"scenario {field_name} must be an uppercase three-letter airport code"
                )
        return self


def load_golden_set(path: str | Path) -> list[GoldenCase]:
    """Parse JSONL and enforce unique IDs, exact family names, and balanced counts."""
    cases: list[GoldenCase] = []
    source = Path(path)
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(GoldenCase.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValidationError) as error:
            message = f"invalid golden-set row at {source}:{line_number}: {error}"
            raise ValueError(message) from error
    identifiers = [case.case_id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("golden case IDs must be unique")
    counts = Counter(case.family for case in cases)
    expected = set(EXPECTED_FAMILIES)
    if set(counts) != expected:
        difference = sorted(set(counts) ^ expected)
        raise ValueError(f"golden families differ from required families: {difference}")
    imbalanced = {family: counts[family] for family in EXPECTED_FAMILIES if counts[family] != 10}
    if imbalanced:
        raise ValueError(f"expected 10 cases per family; got {imbalanced}")
    return cases
