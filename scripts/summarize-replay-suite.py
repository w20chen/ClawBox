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
            "failures": sum(len(row.get("failures", [])) for row in payload.get("runs", [])),
        })
    final_path = output / "suite-summary.json"
    print(json.dumps({
        "output": str(output.resolve()),
        "complete": final_path.is_file(),
        "generated_block_configs": len(configs),
        "completed_blocks": len(summaries),
        "all_completed_blocks_valid": all(
            item["final_state_equal"] and item["failures"] == 0 for item in blocks
        ),
        "blocks": blocks,
        "suite_summary": str(final_path) if final_path.is_file() else None,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
