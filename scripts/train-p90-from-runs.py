#!/usr/bin/env python3
"""Train a repository-level immutable P90 file from trusted prior run artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

from clawbox.cell.p90 import AdmissionPrediction
from clawbox.tuning.__main__ import find_run_traces
from clawbox.tuning.dataset import build_joined_dataset
from clawbox.tuning.native import _clawtune_api


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tenant", default="offline-research")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    _, CompletedCall, RuntimeToolResourceKB, ToolCallQuery, _, _ = _clawtune_api()
    calls = []
    source = hashlib.sha256()
    for run in sorted(args.runs, key=str):
        trace_dir, bridge = find_run_traces(run)
        for path in sorted({bridge, *trace_dir.glob("*.jsonl")}, key=str):
            source.update(str(path).encode() + b"\0" + path.read_bytes() + b"\0")
        _, trusted = build_joined_dataset(trace_dir, bridge)
        for item in trusted:
            if (item.repo_fingerprint or args.repository) != args.repository:
                raise ValueError(f"{run}: observation crosses repository boundary")
            if (item.start_time is None or item.end_time is None
                    or item.cpu_utilization_avg_cores is None or item.rss_peak_bytes is None):
                continue
            calls.append(CompletedCall(
                repo=args.repository, tool_name=item.tool_name or "exec", command=item.command,
                ts_start=item.start_time.timestamp(), ts_end=item.end_time.timestamp(),
                censored=item.exit_code not in (None, 0),
                peak_cpu_cores=float(item.cpu_utilization_avg_cores),
                peak_cpu_cores_eligible=True,
                peak_memory_mb=float(item.rss_peak_bytes) / (1024.0 * 1024.0),
                peak_memory_mb_eligible=True, ambient_before_mb=0.0,
            ))
    if len(calls) < 5:
        raise ValueError(f"only {len(calls)} trusted completed calls; need at least 5")
    kb = RuntimeToolResourceKB.fit_public(calls)
    for call in calls:
        kb.observe_completed_call(call)
    runtime = kb.to_json_obj()
    predictions = kb.query(ToolCallQuery(
        repo=args.repository, tool_name="exec", command=None,
        ts_start=max(time.time(), float(runtime.get("last_query_ts") or 0.0)),
        ambient_before_mb=0.0,
    ))
    latency, cpu, memory = (predictions[name] for name in
                            ("latency_ms", "peak_cpu_cores", "peak_memory_mb"))
    revision = subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parents[2] / "ClawTune"),
         "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    source_digest = source.hexdigest()
    pair_digest = hashlib.sha256(
        source_digest.encode() + json.dumps(runtime, sort_keys=True).encode()
    ).hexdigest()
    payload = {
        "tenant_id": args.tenant, "repo_fingerprint": args.repository,
        "generation": args.generation, "pair_digest": pair_digest,
        "source_digest": source_digest, "artifact_count": len(calls),
        "clawtune_revision": revision,
        "prediction": {
            "latency_p90_sec": float(latency.conditional_p90) / 1000.0,
            "cpu_p90_cores": float(cpu.conditional_p90),
            "memory_p90_bytes": float(memory.conditional_p90) * 1024.0 * 1024.0,
            "evidence_count": min(latency.evidence_count, cpu.evidence_count,
                                  memory.evidence_count),
            "scopes": {"latency": latency.scope, "cpu": cpu.scope,
                       "memory": memory.scope},
            "fallback_paths": {"latency": list(latency.fallback_path),
                               "cpu": list(cpu.fallback_path),
                               "memory": list(memory.fallback_path)},
        },
        "training": {"run_count": len(args.runs), "completed_calls": len(calls)},
    }
    prediction = AdmissionPrediction.from_payload(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".next")
    payload.update(prediction.as_payload())
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
