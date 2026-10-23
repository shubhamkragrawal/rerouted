"""Create the scenario-only blind review packet from two rows per golden family."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def build_packet(source: Path, destination: Path) -> int:
    rows = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_family: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_family[str(row["family"])].append(row)
    if len(by_family) != 8 or any(len(items) != 10 for items in by_family.values()):
        raise ValueError("expected the locked eight-family, ten-cases-per-family golden set")

    selected: list[dict[str, object]] = []
    for family_index, family in enumerate(sorted(by_family)):
        offsets = (1, 3, 6) if family_index < 4 else (1, 6)
        selected.extend(by_family[family][offset] for offset in offsets)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as stream:
        for index, row in enumerate(selected, start=1):
            packet_row = {
                "packet_id": f"blind-{index:03d}",
                "scenario": row["scenario"],
                "as_of_date": row["as_of_date"],
            }
            stream.write(json.dumps(packet_row, sort_keys=True, ensure_ascii=False) + "\n")
    return len(selected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("domains/air_us/golden_set.jsonl"))
    parser.add_argument(
        "--output", type=Path, default=Path("data/eval/blind_review_packet.jsonl")
    )
    args = parser.parse_args()
    count = build_packet(args.source, args.output)
    print(f"wrote {count} scenario-only cases to {args.output}")


if __name__ == "__main__":
    main()
