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
    parser.add_argument("--command", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = json.loads(args.snapshot.read_text(encoding="utf-8"))
    runtime = source["runtime_snapshot"]
    if isinstance(runtime, str):
        runtime = json.loads(runtime)
    _, _, RuntimeToolResourceKB, ToolCallQuery, _, _, LatticeTimeKB = _clawtune_api()
    from clawbox.tuning.clawtune import predict_native_call_load
    kb = RuntimeToolResourceKB.from_json_obj(runtime)
    lattice = source["lattice_snapshot"]
    if isinstance(lattice, str):
        lattice = json.loads(lattice)
    lattice_kb = LatticeTimeKB.from_json_obj(lattice)
    query = ToolCallQuery(
        repo=str(source["repo_fingerprint"]), tool_name="exec", command=args.command,
        ts_start=max(time.time(), float(runtime.get("last_query_ts") or 0.0)),
        memory_measurement="guest_memtotal_minus_memavailable",
    )
    call_load = predict_native_call_load(kb, query, lattice=lattice_kb)
    latency = call_load.targets["duration_ms"]
    cpu = call_load.targets["cpu_avg_cores"]
    memory = call_load.targets["memory_extra_peak_bytes"]
    values = (latency.p90, cpu.p90, memory.p90)
    if any(value is None or not math.isfinite(float(value)) or float(value) <= 0
           for value in values):
        raise ValueError("LatticeKB has no safe positive P90 for this command and measurement")
    payload = {
        key: source[key] for key in (
            "tenant_id", "repo_fingerprint", "generation", "pair_digest",
            "source_digest", "artifact_count", "clawtune_revision",
        )
    }
    payload["prediction"] = {
        "latency_p90_sec": float(latency.p90) / 1000.0,
        "cpu_p90_cores": float(cpu.p90),
        "memory_p90_bytes": float(memory.p90),
        "evidence_count": min(latency.sample_count, cpu.sample_count, memory.sample_count),
        "backends": {"latency": latency.backend, "cpu": cpu.backend, "memory": memory.backend},
        "contexts": {"latency": list(latency.context), "cpu": list(cpu.context), "memory": list(memory.context)},
    }
    prediction = AdmissionPrediction.from_payload(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".next")
    temporary.write_text(
        json.dumps({**prediction.as_payload(), "command": args.command,
                    "call_prediction": call_load.model_dump(mode="json")}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
