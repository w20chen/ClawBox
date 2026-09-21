"""Build exact pre-tool replay plans for the three requested TerminalBench calls."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


TRACE_ROOT = Path(r"C:\Users\29068\Desktop\downloaded-runs\pair-20260913-201717-traces\terminal\traces")
OUT = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\llc-placement-discovery-20260914\terminal_pretool_plans.json")

TARGETS = [
    {
        "name": "3d-model-format-legacy",
        "label": "TerminalBench 3d-model-format-legacy",
        "trace": "bb776dff4125a46c4137",
        "span": "call_00_sansaVJ8fmWIyFbMb3UN8705",
        "base_image": "placement-terminal-a-base:20260914",
        "workdir": "/app",
        "duration_s": 34.314,
        "llc_read_M_per_CPU_s": 7.683459,
        "llc_miss_M_per_CPU_s": 1.380670,
        "H_M_per_CPU_s": 6.302789,
    },
    {
        "name": "accelerate-maximal-square",
        "label": "TerminalBench accelerate-maximal-square",
        "trace": "0ac9da1472b548f927a0",
        "span": "call_00_m8y9K8gmxLy9MZvrUygG6421",
        "base_image": "placement-terminal-b-base:20260914",
        "workdir": "/app",
        "duration_s": 65.774,
        "llc_read_M_per_CPU_s": 4.232675,
        "llc_miss_M_per_CPU_s": 0.559144,
        "H_M_per_CPU_s": 3.673531,
    },
    {
        "name": "blind-maze-explorer-algorithm",
        "label": "TerminalBench blind-maze-explorer-algorithm",
        "trace": "39070641548c52ea38a2",
        "span": "call_00_5QNpjEGAIN8cmLgJxCTP5539",
        "base_image": "placement-terminal-c-base:20260914",
        "workdir": "/app",
        "duration_s": 29.791,
        "llc_read_M_per_CPU_s": 4.638358,
        "llc_miss_M_per_CPU_s": 0.879932,
        "H_M_per_CPU_s": 3.758426,
    },
]


def inner_output(end: dict) -> dict:
    content = end.get("output", {}).get("result", {}).get("content", [])
    for item in content:
        if item.get("type") == "text":
            try:
                value = json.loads(item.get("text", ""))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "stdout" in value:
                return value
    raise ValueError(f"no terminal output payload in span {end.get('span_id')}")


def extract(target: dict) -> dict:
    trace_path = TRACE_ROOT / target["trace"] / "trace.jsonl"
    starts: dict[str, dict] = {}
    completed: list[dict] = []
    target_start_sequence = None
    target_end = None

    for line in trace_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("record_type") == "span_start" and row.get("name") == "terminal_exec":
            starts[row["span_id"]] = row
            if row["span_id"] == target["span"]:
                target_start_sequence = row.get("sequence_no")
        elif row.get("record_type") == "span_end" and row.get("name") == "terminal_exec":
            sid = row.get("span_id")
            start = starts.get(sid)
            if not start:
                continue
            requested_args = (start.get("input") or {}).get("requested_args") or {}
            item = {
                "span_id": sid,
                "sequence_no": start.get("sequence_no"),
                "command": requested_args.get("command"),
                "status": row.get("status", {}).get("code"),
            }
            if sid == target["span"]:
                target_end = inner_output(row)
            elif target_start_sequence is None or item["sequence_no"] < target_start_sequence:
                if item["command"]:
                    completed.append(item)

    if target_start_sequence is None or target_end is None:
        raise RuntimeError(f"target span not found: {target['span']} in {trace_path}")

    stdout = target_end.get("stdout", "")
    target = dict(target)
    # The target command is held in the start record; recover it with a second pass
    # without relying on end-event ordering.
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("record_type") == "span_start" and row.get("span_id") == target["span"]:
            target["command"] = row["input"]["requested_args"]["command"]
            break
    target.update(
        {
            "expected_exit_code": int(target_end.get("exit_code", 0)),
            "expected_stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
            "expected_stdout_tail": stdout.rstrip().splitlines()[-1] if stdout.rstrip() else "",
            "prefix": sorted(completed, key=lambda item: item["sequence_no"]),
        }
    )
    return target


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "llc-placement-terminal-pretool-v1",
        "created_from": str(TRACE_ROOT),
        "targets": [extract(target) for target in TARGETS],
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for target in payload["targets"]:
        print(
            target["name"],
            "prefix_commands=",
            len(target["prefix"]),
            "target_chars=",
            len(target["command"]),
            "target_sha256=",
            target["expected_stdout_sha256"],
        )
    print(OUT)


if __name__ == "__main__":
    main()
