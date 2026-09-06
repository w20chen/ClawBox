#!/usr/bin/env python3
"""Freeze exact-command P90 and held-out oracle inputs for the selected rec-a trace."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def selected_commands(path: Path) -> list[str]:
    commands: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        for call in row["data"]["raw_response"].get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") != "exec":
                continue
            arguments = json.loads(function["arguments"])
            command = arguments.get("command")
            if isinstance(command, str) and command not in commands:
                commands.append(command)
    return commands


def heldout_memory(trace_dir: Path) -> dict[str, int]:
    by_execution: dict[str, int] = {}
    for path in (trace_dir / "tool-resource").glob("cgroup-resource-*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        execution_id = row.get("execution_id")
        peak = row.get("memory_rss_peak_bytes")
        if execution_id and isinstance(peak, (int, float)) and peak > 0:
            by_execution[str(execution_id)] = int(peak)

    commands: dict[str, list[int]] = {}
    for path in trace_dir.glob("*.jsonl"):
        starts: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("name") != "exec":
                continue
            span_id = str(row.get("span_id") or "")
            if row.get("record_type") == "span_start":
                command = ((row.get("input") or {}).get("requested_args") or {}).get("command")
                if isinstance(command, str):
                    starts[span_id] = command
            elif row.get("record_type") == "span_end" and span_id in starts:
                execution_id = (row.get("execution") or {}).get("execution_id")
                if execution_id in by_execution:
                    commands.setdefault(starts[span_id], []).append(by_execution[execution_id])
    return {command: max(values) for command, values in commands.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selected-trace", type=Path, required=True)
    parser.add_argument("--heldout-trace-dir", type=Path, required=True)
    parser.add_argument("--training-summary", type=Path, required=True)
    parser.add_argument("--p90-output", type=Path, required=True)
    parser.add_argument("--oracle-output", type=Path, required=True)
    args = parser.parse_args()

    commands = selected_commands(args.selected_trace)
    measured = heldout_memory(args.heldout_trace_dir)
    training = json.loads(args.training_summary.read_text(encoding="utf-8"))
    p90_guest_mib = float(training["prediction"]["memory_p90_bytes"]) / 2**20
    configured_tool_mib = 4096.0
    conservative_host_ratio = configured_tool_mib / p90_guest_mib

    common = {
        "schema": "clawbox_command_memory_v1",
        "repository": "15five/scim2-filter-parser",
        "selected_trace": {"path": str(args.selected_trace), "sha256": sha256(args.selected_trace)},
    }
    p90_entries = []
    oracle_entries = []
    missing = []
    for command in commands:
        digest = hashlib.sha256(command.encode()).hexdigest()
        p90_entries.append({
            "command": command,
            "command_sha256": digest,
            "predicted_command_memory_p90_mib": p90_guest_mib,
            "predicted_host_execution_increment_mib": configured_tool_mib,
            "host_mapping_source": "configured_tool_memory_conservative_upper_bound",
            "host_mapping_ratio_p90": conservative_host_ratio,
            "key_kind": "exact_command",
            "fallback_path": ["repo:tool_name", "configured_tool_memory_upper_bound"],
            "evidence_count": int(training["prediction"]["evidence_count"]),
        })
        peak = measured.get(command)
        if peak is None:
            missing.append(digest)
            oracle_guest_mib = p90_guest_mib
            fallback = "heldout_missing_conservative_full"
        else:
            oracle_guest_mib = peak / 2**20
            fallback = "exact_command_heldout"
        oracle_entries.append({
            "command": command,
            "command_sha256": digest,
            "predicted_command_memory_p90_mib": oracle_guest_mib,
            "predicted_host_execution_increment_mib": min(
                configured_tool_mib, math.ceil(oracle_guest_mib * conservative_host_ratio * 1024) / 1024
            ),
            "host_mapping_source": "heldout_rec_a_guest_peak_with_conservative_host_ratio",
            "host_mapping_ratio_p90": conservative_host_ratio,
            "key_kind": fallback,
            "fallback_path": [fallback],
            "evidence_count": 1 if peak is not None else 0,
        })

    p90 = {
        **common,
        "kind": "independent_recording_p90",
        "training_summary": {"path": str(args.training_summary), "sha256": sha256(args.training_summary)},
        "tool_invocations": p90_entries,
    }
    oracle = {
        **common,
        "kind": "heldout_rec_a_tool_oracle",
        "heldout_trace_dir": str(args.heldout_trace_dir),
        "missing_command_sha256": missing,
        "tool_invocations": oracle_entries,
    }
    for path, payload in ((args.p90_output, p90), (args.oracle_output, oracle)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "command_count": len(commands),
        "heldout_exact_count": len(commands) - len(missing),
        "heldout_missing_count": len(missing),
        "p90_sha256": sha256(args.p90_output),
        "oracle_sha256": sha256(args.oracle_output),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
