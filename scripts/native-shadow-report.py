#!/usr/bin/env python3
"""Join native ClawTune predictions with Tool-VM actuals and KB provenance."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any


def _records(trace_dir: Path):
    for path in sorted(trace_dir.glob("*.jsonl")):
        if path.name == "tool-bridge.jsonl":
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def _actuals(trace_dir: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted((trace_dir / "tool-resource").glob("cgroup-resource-*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        execution_id = value.get("execution_id") if isinstance(value, dict) else None
        if isinstance(execution_id, str) and value.get("source") == "cgroup-v2":
            result[execution_id] = value
    return result


def _match(prediction: dict[str, Any]) -> tuple[str | None, int]:
    primary = prediction.get("prediction")
    candidates = [primary] if isinstance(primary, dict) else []
    candidates.extend(
        item["prediction"]
        for item in prediction.get("clause_predictions") or []
        if isinstance(item, dict) and isinstance(item.get("prediction"), dict)
    )
    if not candidates:
        return None, 0
    best = max(candidates, key=lambda item: int(item.get("evidence_count") or 0))
    scope = best.get("scope")
    kind = best.get("key_kind")
    return (f"{scope}:{kind}" if scope and kind else scope or kind), int(
        best.get("evidence_count") or 0
    )


def build_report(trace_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    actuals = _actuals(trace_dir)
    rows: list[dict[str, Any]] = []
    evidence_runs = list((metadata.get("evidence") or {}).get("runs") or [])
    for record in _records(trace_dir):
        if record.get("record_type") != "span_start":
            continue
        prediction = (record.get("prediction") or {}).get("tool_resource")
        execution_id = (record.get("execution") or {}).get("execution_id")
        if not isinstance(prediction, dict) or not isinstance(execution_id, str):
            continue
        actual = actuals.get(execution_id)
        if actual is None:
            continue
        ts_start = float(actual["ts_start"])
        ts_end = float(actual["ts_end"])
        cpu = float(actual["cpu_utilization_avg_cores"])
        rss = int(actual["memory_rss_peak_bytes"])
        if not all(math.isfinite(value) for value in (ts_start, ts_end, cpu)):
            continue
        match_level, evidence_count = _match(prediction)
        rows.append({
            "execution_id": execution_id,
            "tenant_id": metadata["tenant_id"],
            "repo_fingerprint": metadata["repo_fingerprint"],
            "run_id": os.getenv("CLAWBOX_RUN_ID", record.get("run_id") or ""),
            "attempt_id": os.getenv("CLAWBOX_ATTEMPT_ID", ""),
            "kb_generation": metadata["generation"],
            "snapshot_pair_digest": metadata["pair_digest"],
            "source_digest": metadata["source_digest"],
            "evidence_runs": evidence_runs,
            "match_level": match_level,
            "evidence_count": evidence_count,
            "predicted_values": prediction,
            "actual_values": {
                "duration_ms": (ts_end - ts_start) * 1000.0,
                "cpu_utilization_avg_cores": cpu,
                "memory_rss_peak_bytes": rss,
                "sampling_quality": actual.get("sampling_quality"),
                "source": actual.get("source"),
            },
        })
    if not rows:
        raise ValueError("no native predictions joined to Tool-VM cgroup actuals")
    return {
        "schema": "clawbox.native_shadow_report_v1",
        "clawbox_revision": os.getenv("CLAWBOX_REVISION", "unknown"),
        "clawtune_revision": metadata["clawtune_revision"],
        "resource_control": {
            "authoritative_sizer": "FixedProfileSizer",
            "profile": os.getenv("CLAWBOX_RESOURCE_PROFILE", ""),
            "prediction_mode": "shadow",
            "prediction_controls_resources": False,
        },
        "prediction_count": len(rows),
        "predictions": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--kb-metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metadata = json.loads(args.kb_metadata.read_text(encoding="utf-8"))
    report = build_report(args.trace_dir, metadata)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(args.output)
    print(json.dumps({
        "prediction_count": report["prediction_count"],
        "generation": report["predictions"][0]["kb_generation"],
        "evidence_runs": report["predictions"][0]["evidence_runs"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
