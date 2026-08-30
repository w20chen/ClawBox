from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run(root: Path) -> dict[str, object]:
    script = Path(__file__).resolve().parents[1] / "scripts" / "summarize-replay-suite.py"
    completed = subprocess.run(
        [sys.executable, str(script), str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_progress_summary_includes_partial_arm_correctness(tmp_path: Path) -> None:
    root = tmp_path / "suite"
    arm = root / "runs" / "trace-a" / "c08" / "r00-replay-fixed-resident" / "results"
    arm.mkdir(parents=True)
    (root / "config-trace-a-c08.json").write_text("{}\n", encoding="utf-8")
    (arm / "summary.json").write_text(json.dumps({
        "sessions_requested": 8,
        "sessions_completed": 8,
        "correctness_evaluated": True,
        "correct_sessions_completed": 8,
        "model_steps_completed": 216,
        "failures": [],
    }), encoding="utf-8")
    report = _run(root)
    assert report["complete"] is False
    assert report["completed_blocks"] == 0
    assert report["completed_arm_repetitions"] == 1
    assert report["completed_arm_sessions"] == 8
    assert report["completed_arm_correct_sessions"] == 8
    assert report["completed_arm_model_steps"] == 216
    assert report["all_completed_arms_valid"] is True
    assert report["all_completed_blocks_valid"] is None
    assert report["arms"][0]["workload"] == "trace-a"
    assert report["arms"][0]["concurrency"] == 8


def test_progress_summary_flags_incorrect_completed_arm(tmp_path: Path) -> None:
    root = tmp_path / "suite"
    arm = root / "runs" / "trace-a" / "c01" / "r00-replay-fixed-resident" / "results"
    arm.mkdir(parents=True)
    (arm / "summary.json").write_text(json.dumps({
        "sessions_requested": 1,
        "sessions_completed": 1,
        "correctness_evaluated": True,
        "correct_sessions_completed": 0,
        "model_steps_completed": 27,
        "failures": [],
    }), encoding="utf-8")
    report = _run(root)
    assert report["all_completed_arms_valid"] is False
    assert len(report["invalid_completed_arms"]) == 1


def test_progress_summary_does_not_claim_empty_outputs_are_valid(tmp_path: Path) -> None:
    report = _run(tmp_path / "empty")
    assert report["all_completed_arms_valid"] is None
    assert report["all_completed_blocks_valid"] is None
