"""Create repeated mapped tasks for an infrastructure-only concurrency smoke."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from clawbox.benchmark.kubernetes import load_arm64_mapping, load_tasks


def build_smoke_tasks(tasks_path: Path, mapping_path: Path, count: int) -> list[dict[str, str]]:
    if count < 1:
        raise ValueError("count must be >= 1")
    mapping = load_arm64_mapping(mapping_path)
    source = next((task for task in load_tasks(tasks_path) if task.image in mapping), None)
    if source is None:
        raise ValueError("the task file has no entry supported by the ARM64 mapping")
    return [
        {
            "instance_id": f"{source.instance_id}-concurrency-{index:02d}",
            "image": source.image,
            "problem_statement": "Inspect the repository and report readiness without modifying files.\n",
            "base_commit": source.base_commit,
            "hint_text": "infrastructure concurrency smoke; not a benchmark score",
        }
        for index in range(1, count + 1)
    ]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--arm64-map", type=Path, required=True)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        records = build_smoke_tasks(args.tasks, args.arm64_map, args.count)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(records)} concurrency-smoke tasks to {args.output}")
