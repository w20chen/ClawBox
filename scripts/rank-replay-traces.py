#!/usr/bin/env python3
"""Rank trace files by replay suitability and LLM-idle fraction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from clawbox.replay.trace import load_trace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--min-actions", type=int, default=10)
    args = parser.parse_args()

    rows: list[dict[str, object]] = []
    for root in args.roots:
        paths = [root] if root.is_file() else root.rglob("*.jsonl")
        for path in paths:
            try:
                actions = load_trace(path)
            except (OSError, ValueError):
                continue
            if len(actions) < args.min_actions:
                continue
            llms = [action for action in actions if action.kind == "llm"]
            tools = [action for action in actions if action.kind == "tool"]
            replayable = 0
            unreplayable: list[dict[str, str]] = []
            for action in tools:
                try:
                    action.shell_command()
                    replayable += 1
                except ValueError as exc:
                    unreplayable.append({
                        "action_id": action.action_id,
                        "name": action.name,
                        "reason": str(exc),
                    })
            llm_s = sum(action.duration_s for action in llms)
            tool_s = sum(action.duration_s for action in tools)
            rows.append({
                "path": str(path),
                "actions": len(actions),
                "llms": len(llms),
                "tools": len(tools),
                "replayable_tools": replayable,
                "unreplayable": unreplayable,
                "llm_s": llm_s,
                "tool_s": tool_s,
                "llm_fraction": llm_s / (llm_s + tool_s) if llm_s + tool_s else 0.0,
                "max_llm_s": max((action.duration_s for action in llms), default=0.0),
            })
    rows.sort(
        key=lambda row: (
            row["replayable_tools"] == row["tools"],
            row["llm_fraction"],
            row["llm_s"],
        ),
        reverse=True,
    )
    print(json.dumps(rows[: args.limit], indent=2))


if __name__ == "__main__":
    main()
