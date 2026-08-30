#!/usr/bin/env python3
"""Materialize prompts for the exact SWE-ReBench images present in a mapping."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--arm64-map", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    records = json.loads(args.tasks.read_text(encoding="utf-8"))
    if isinstance(records, dict):
        records = records.get("tasks", records.get("instances", []))
    mapping = json.loads(args.arm64_map.read_text(encoding="utf-8"))
    by_image = {
        str(item.get("docker_image") or item.get("image")): item for item in records
    }
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for original, image in sorted(mapping.items()):
        task = by_image.get(original)
        if task is None:
            raise ValueError(f"mapped image has no selected task: {original}")
        instance = str(task["instance_id"])
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", instance).strip("-")
        problem = str(task.get("problem_statement") or "").strip()
        hint = str(task.get("hint_text") or "").strip()
        if not problem:
            raise ValueError(f"{instance}: problem statement is empty")
        prompt = args.output / f"{safe}.prompt.txt"
        prompt.write_text(problem + (("\n\nHint:\n" + hint) if hint else "") + "\n",
                          encoding="utf-8")
        mapped = image["arm64_image"] if isinstance(image, dict) else image
        manifest.append({"name": safe, "instance_id": instance,
                         "repository": task.get("repo"), "original_image": original,
                         "arm64_image": mapped, "prompt": str(prompt)})
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
