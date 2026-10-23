import json
from pathlib import Path

from scripts.build_blind_review_packet import build_packet

ROOT = Path(__file__).parents[1]


def test_packet_has_twenty_scenario_only_cases_and_no_labels(tmp_path: Path) -> None:
    output = tmp_path / "blind.jsonl"
    count = build_packet(ROOT / "domains/air_us/golden_set.jsonl", output)
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

    assert count == 20
    assert len(rows) == 20
    assert [row["packet_id"] for row in rows] == [f"blind-{i:03d}" for i in range(1, 21)]
    assert all(set(row) == {"packet_id", "scenario", "as_of_date"} for row in rows)
    assert all("expected" not in row and "family" not in row for row in rows)
