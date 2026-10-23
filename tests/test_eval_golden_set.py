import json
from pathlib import Path

import pytest

from core.eval import CasePrediction, evaluate_answers, load_golden_set
from domains.air_us.rules.entitlement import (
    ConsumerDisposition,
    DisruptionFacts,
    ItineraryKind,
    resolve_air_travel,
)

GOLDEN = Path(__file__).parents[1] / "domains" / "air_us" / "golden_set.jsonl"


def test_golden_set_has_eight_balanced_families_and_source_provenance() -> None:
    cases = load_golden_set(GOLDEN)
    assert len(cases) == 80
    family_names = {case.family for case in cases}
    counts = {family: sum(case.family == family for case in cases) for family in family_names}
    assert counts == {family: 10 for family in family_names}
    assert all(case.source_of_truth for case in cases)


def test_golden_schema_rejects_missing_family_case(tmp_path: Path) -> None:
    lines = GOLDEN.read_text(encoding="utf-8").splitlines()
    path = tmp_path / "short.jsonl"
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="10 cases per family"):
        load_golden_set(path)


def test_hand_authored_expected_answers_match_deterministic_resolver() -> None:
    cases = load_golden_set(GOLDEN)
    predictions: dict[str, CasePrediction] = {}
    for case in cases:
        scenario = case.scenario
        itinerary = scenario.get("itinerary_kind")
        facts = DisruptionFacts(
            as_of_date=case.as_of_date,
            covered_flight=scenario.get("covered_flight"),
            merchant_of_record=scenario.get("merchant_of_record"),
            itinerary_kind=ItineraryKind(itinerary) if itinerary is not None else None,
            consumer_disposition=ConsumerDisposition(
                scenario.get("consumer_disposition", "unknown")
            ),
            cancelled=scenario.get("cancelled"),
            flight_number_changed=scenario.get("flight_number_changed"),
            rebooked_on_renumbered_flight=scenario.get("rebooked_on_renumbered_flight"),
            departure_earlier_minutes=scenario.get("departure_earlier_minutes"),
            arrival_later_minutes=scenario.get("arrival_later_minutes"),
            origin_or_destination_changed=scenario.get("origin_or_destination_changed"),
            added_connection=scenario.get("added_connection"),
            downgraded_class=scenario.get("downgraded_class"),
            passenger_has_disability=scenario.get("passenger_has_disability"),
            disability_connection_changed=scenario.get("disability_connection_changed"),
            needed_accessibility_feature_unavailable=scenario.get(
                "needed_accessibility_feature_unavailable"
            ),
        )
        resolved = resolve_air_travel(facts)
        expected = case.expected
        assert resolved.entitlement.status.value == expected.entitlement, case.case_id
        expected_posture = expected.enforcement_posture
        assert resolved.enforcement.status.value == expected_posture.status, case.case_id
        assert resolved.enforcement.regime_id == expected_posture.regime_id, case.case_id
        assert resolved.enforcement.checked is expected_posture.checked, case.case_id
        assert tuple(resolved.enforcement.applicable_notice_ids) == expected_posture.notice_ids, (
            case.case_id
        )
        assert set(resolved.entitlement.governing_clause_ids) == set(expected.governing_clauses), (
            case.case_id
        )
        assert expected.expected_refusal is bool(resolved.entitlement.unresolved_facts), (
            case.case_id
        )
        predictions[case.case_id] = CasePrediction(
            case_id=case.case_id,
            entitlement=resolved.entitlement.status.value,
            enforcement_posture={
                "status": resolved.enforcement.status.value,
                "regime_id": resolved.enforcement.regime_id,
                "checked": resolved.enforcement.checked,
                "notice_ids": list(resolved.enforcement.applicable_notice_ids),
                "obligation_preserved": True,
            },
            cited_clauses=resolved.entitlement.governing_clause_ids,
            refusal=bool(resolved.entitlement.unresolved_facts),
            refusal_reason="insufficient_material_facts"
            if resolved.entitlement.unresolved_facts
            else None,
        )
    # These perfect scores assert resolver/golden consistency only, not system accuracy.
    score = evaluate_answers(cases, predictions)
    assert score["entitlement_accuracy"]["overall"] == 1.0
    assert score["posture_correctness"]["accuracy"] == 1.0
    assert score["obligation_enforcement_conflation"]["zero_tolerance_pass"] is True
    assert score["citation"]["precision"] == 1.0
    assert score["citation"]["recall"] == 1.0
    assert score["refusal"]["accuracy"] == 1.0


def test_golden_jsonl_rows_are_independently_parseable() -> None:
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        assert isinstance(json.loads(line), dict)
