#!/usr/bin/env python3
"""Write a small two-VM continuity trace for a disposable workspace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def action(kind: str, action_id: str, start: float, end: float, data: dict) -> dict:
    return {
        "type": "action", "action_type": kind, "action_id": action_id,
        "iteration": int(start), "ts_start": start, "ts_end": end, "data": data,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--wait-s", type=float, default=4.0)
    args = parser.parse_args()
    if args.wait_s <= 0:
        parser.error("--wait-s must be positive")
    llm = lambda action_id, start, text: action("llm_call", action_id, start, start + args.wait_s, {
        "messages_in": [{"role": "user", "content": text}],
        "raw_response": {"content": action_id},
        "llm_latency_ms": round(args.wait_s * 1000),
    })
    first_end = args.wait_s
    records = [
        llm("llm-1", 0, "first direct Firecracker request with sufficient length"),
        action("tool_exec", "tool-1", first_end, first_end + 0.1, {
            "tool_name": "exec", "args": {"command": "printf vm-ok > /workspace/result.txt"},
            "exit_code": 0, "result": "",
        }),
        llm("llm-2", first_end + 0.1, "second direct Firecracker request with sufficient length"),
        action("tool_exec", "tool-2", first_end * 2 + 0.1, first_end * 2 + 0.2, {
            "tool_name": "exec", "args": {"command": "grep -qx vm-ok /workspace/result.txt"},
            "exit_code": 0, "result": "",
        }),
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


if __name__ == "__main__":
    main()
