#!/usr/bin/env python3
"""Summarize a flat SWE-ReBench trace corpus for reproducible selection."""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(q * len(ordered)) - 1]


def summarize(path: Path, manifest: dict[str, dict]) -> dict:
    metadata = None
    waits: list[float] = []
    tools: dict[str, int] = {}
    starts: list[float] = []
    ends: list[float] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("type") == "trace_metadata":
                metadata = row
            if row.get("type") != "action" or row.get("action_type") != "llm_call":
                continue
            start, end = float(row["ts_start"]), float(row["ts_end"])
            if not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise ValueError(f"{path}: invalid LLM timestamps")
            waits.append(end - start)
            starts.append(start)
            ends.append(end)
            choices = row["data"]["raw_response"]["choices"]
            if len(choices) != 1:
                raise ValueError(f"{path}: expected one model choice")
            for call in choices[0]["message"].get("tool_calls", []):
                name = str((call.get("function") or {}).get("name") or "")
                tools[name] = tools.get(name, 0) + 1
    if not metadata or metadata.get("benchmark") != "swe-rebench" or not waits:
        raise ValueError(f"{path}: not a nonempty SWE-ReBench recording")
    item = manifest.get(path.name, {})
    instance = str(metadata.get("instance_id") or item.get("instance_id") or path.stem)
    return {
        "file": path.name,
        "corpus": item.get("corpus"),
        "instance_id": instance,
        "repository": instance.split("__", 1)[0],
        "model": metadata.get("model") or item.get("model"),
        "status": item.get("status"),
        "trace_bytes": path.stat().st_size,
        "model_calls": len(waits),
        "model_wait_total_s": sum(waits),
        "model_wait_mean_s": statistics.fmean(waits),
        "model_wait_p50_s": percentile(waits, .50),
        "model_wait_p95_s": percentile(waits, .95),
        "model_wait_max_s": max(waits),
        "recorded_task_span_s": max(ends) - min(starts),
        "tool_calls": sum(tools.values()),
        "exec_calls": tools.get("exec", 0),
        "read_calls": tools.get("read_file", 0) + tools.get("read", 0),
        "write_edit_calls": sum(tools.get(k, 0) for k in ("write_file", "edit_file", "write", "edit")),
        "tool_distribution": json.dumps(tools, sort_keys=True, separators=(",", ":")),
        "telemetry_quality": metadata.get("telemetry_quality"),
        "formal_completeness": metadata.get("formal_completeness"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = {}
    with (args.corpus / "MANIFEST.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            manifest[row["flattened_name"]] = row
    rows = [summarize(path, manifest) for path in sorted(args.corpus.glob("*.trace.jsonl"))]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"trace_count": len(rows), "output": str(args.output.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
