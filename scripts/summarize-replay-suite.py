#!/usr/bin/env python3
"""Read-only progress/result summary for a long replay suite."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output
    configs = sorted(output.glob("config-*.json"))
    summaries = sorted((output / "runs").glob("*/c*/study-summary.json"))
    arm_summaries = sorted((output / "runs").glob("*/c*/r*/results/summary.json"))
    blocks = []
    for path in summaries:
        payload = json.loads(path.read_text(encoding="utf-8"))
        blocks.append({
            "workload": path.parents[1].name,
            "concurrency": int(path.parent.name.removeprefix("c")),
            "final_state_equal": bool(payload.get("final_state_equal")),
            "arm_repetitions": len(payload.get("runs", [])),
            "sessions_completed": sum(
                int(row.get("sessions_completed", 0)) for row in payload.get("runs", [])
            ),
            "correct_sessions_completed": sum(
                int(row.get("correct_sessions_completed", 0))
                for row in payload.get("runs", [])
            ),
            "failures": sum(len(row.get("failures", [])) for row in payload.get("runs", [])),
        })
    arms = []
    for path in arm_summaries:
        payload = json.loads(path.read_text(encoding="utf-8"))
        requested = int(payload.get("sessions_requested", 0))
        completed = int(payload.get("sessions_completed", 0))
        correctness_evaluated = bool(payload.get("correctness_evaluated"))
        correct = int(payload.get("correct_sessions_completed", 0))
        failures = len(payload.get("failures", []))
        valid = (
            requested > 0 and completed == requested and failures == 0
            and (not correctness_evaluated or correct == requested)
        )
        arms.append({
            "workload": path.parents[3].name,
            "concurrency": int(path.parents[2].name.removeprefix("c")),
            "arm": path.parents[1].name,
            "sessions_requested": requested,
            "sessions_completed": completed,
            "correct_sessions_completed": correct,
            "correctness_evaluated": correctness_evaluated,
            "model_steps_completed": int(payload.get("model_steps_completed", 0)),
            "failures": failures,
            "valid": valid,
        })
    final_path = output / "suite-summary.json"
    print(json.dumps({
        "output": str(output.resolve()),
        "complete": final_path.is_file(),
        "generated_block_configs": len(configs),
        "completed_blocks": len(summaries),
        "completed_arm_repetitions": len(arms),
        "completed_arm_sessions": sum(item["sessions_completed"] for item in arms),
        "completed_arm_correct_sessions": sum(
            item["correct_sessions_completed"] for item in arms
        ),
        "completed_arm_model_steps": sum(item["model_steps_completed"] for item in arms),
        "all_completed_arms_valid": (
            all(item["valid"] for item in arms) if arms else None
        ),
        "invalid_completed_arms": [item for item in arms if not item["valid"]],
        "all_completed_blocks_valid": (
            all(item["final_state_equal"] and item["failures"] == 0 for item in blocks)
            if blocks else None
        ),
        "blocks": blocks,
        "arms": arms,
        "suite_summary": str(final_path) if final_path.is_file() else None,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
