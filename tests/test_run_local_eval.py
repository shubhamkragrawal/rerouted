from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from scripts import run_local_eval


@pytest.mark.parametrize(
    ("ci_status", "expected_exit"),
    [("fail", 1), ("pass", 0), ("pending_baseline_or_measurements", 0)],
)
def test_main_exit_code_tracks_ci_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ci_status: str,
    expected_exit: int,
) -> None:
    report: dict[str, Any] = {
        "result_version": "unit-test",
        "answers": None,
        "retrieval_report": {},
        "operations": {},
        "ci": {"status": ci_status},
    }
    monkeypatch.setattr(run_local_eval, "run_eval", lambda **_: report)
    monkeypatch.setattr(sys, "argv", ["run_local_eval.py"])

    if expected_exit:
        with pytest.raises(SystemExit) as exc_info:
            run_local_eval.main()
        assert exc_info.value.code == expected_exit
    else:
        run_local_eval.main()

    output = json.loads(capsys.readouterr().out)
    assert output["ci_status"] == ci_status
