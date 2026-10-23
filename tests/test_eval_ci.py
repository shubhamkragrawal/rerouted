from pathlib import Path

from core.eval import CasePrediction, evaluate_answers, evaluate_ci, load_golden_set

GOLDEN = Path(__file__).parents[1] / "domains" / "air_us" / "golden_set.jsonl"


def test_deliberately_broken_prediction_fails_hard_citation_gate() -> None:
    case = load_golden_set(GOLDEN)[0]
    posture = case.expected.enforcement_posture
    broken = CasePrediction(
        case_id=case.case_id,
        entitlement=case.expected.entitlement,
        enforcement_posture={
            "status": posture.status,
            "regime_id": posture.regime_id,
            "checked": posture.checked,
            "notice_ids": list(posture.notice_ids),
            "obligation_preserved": posture.obligation_preserved,
        },
        cited_clauses=(),  # Deliberate grounding failure.
        refusal=case.expected.expected_refusal,
    )
    metrics = evaluate_answers([case], {case.case_id: broken})

    result = evaluate_ci(metrics)

    assert metrics["citation"]["precision"] == 0.0
    assert result["status"] == "fail"
    assert "citation_precision_below_0.90" in result["failures"]
