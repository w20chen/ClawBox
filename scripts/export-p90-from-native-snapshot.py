#!/usr/bin/env python3
"""Export a replay-study P90 decision from a signed native KB snapshot."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

from clawbox.cell.p90 import AdmissionPrediction
from clawbox.tuning.native import _clawtune_api


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = json.loads(args.snapshot.read_text(encoding="utf-8"))
    runtime = source["runtime_snapshot"]
    if isinstance(runtime, str):
        runtime = json.loads(runtime)
    _, _, RuntimeToolResourceKB, ToolCallQuery, _, _ = _clawtune_api()
    predictions = RuntimeToolResourceKB.from_json_obj(runtime).query(ToolCallQuery(
        repo=str(source["repo_fingerprint"]), tool_name="exec", command=None,
        ts_start=max(time.time(), float(runtime.get("last_query_ts") or 0.0)),
        ambient_before_mb=0.0,
    ))
    latency = predictions["latency_ms"]
    cpu = predictions["peak_cpu_cores"]
    memory = predictions["peak_memory_mb"]
    values = (latency.conditional_p90, cpu.conditional_p90, memory.conditional_p90)
    if any(value is None or not math.isfinite(float(value)) or float(value) <= 0
           for value in values):
        raise ValueError("native snapshot has no safe positive p90")
    payload = {
        key: source[key] for key in (
            "tenant_id", "repo_fingerprint", "generation", "pair_digest",
            "source_digest", "artifact_count", "clawtune_revision",
        )
    }
    payload["prediction"] = {
        "latency_p90_sec": float(latency.conditional_p90) / 1000.0,
        "cpu_p90_cores": float(cpu.conditional_p90),
        "memory_p90_bytes": float(memory.conditional_p90) * 1024.0 * 1024.0,
        "evidence_count": min(latency.evidence_count, cpu.evidence_count,
                              memory.evidence_count),
        "scopes": {"latency": latency.scope, "cpu": cpu.scope, "memory": memory.scope},
        "fallback_paths": {"latency": list(latency.fallback_path),
                           "cpu": list(cpu.fallback_path),
                           "memory": list(memory.fallback_path)},
    }
    prediction = AdmissionPrediction.from_payload(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".next")
    temporary.write_text(
        json.dumps(prediction.as_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
