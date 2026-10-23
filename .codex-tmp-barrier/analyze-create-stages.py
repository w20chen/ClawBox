from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


event_path = Path(sys.argv[1]).resolve()
rows: list[dict] = []
first_wall_ns: int | None = None
event_count = 0
with event_path.open(encoding="utf-8") as stream:
    for line in stream:
        if not line.strip():
            continue
        event = json.loads(line)
        event_count += 1
        wall_ns = int(event["wall_time_ns"])
        if first_wall_ns is None:
            first_wall_ns = wall_ns
        if event.get("event") != "sandbox_created":
            continue
        timing = event.get("lifecycle_timing") or {}
        stages = timing.get("stage_seconds")
        if not isinstance(stages, dict):
            continue
        service = float(timing["service_seconds"])
        inside_service = sum(
            float(stages.get(name, 0.0))
            for name in (
                "physical_observation_before_s", "prepare_cgroup_s",
                "api_create_s", "verify_cgroup_s", "pin_local_s",
            )
        )
        rows.append({
            "elapsed_s": (wall_ns - first_wall_ns) / 1_000_000_000,
            "session_id": event.get("session_id"),
            "role": event.get("role", "unknown"),
            "started_unix_s": float(timing["started_unix_s"]),
            "completed_unix_s": float(timing["completed_unix_s"]),
            "service_seconds": service,
            "unattributed_inside_service_s": max(0.0, service - inside_service),
            **{name: float(value) for name, value in stages.items()},
        })

metrics = [
    "service_seconds", "physical_observation_before_s", "prepare_cgroup_s",
    "api_create_s", "verify_cgroup_s", "pin_local_s",
    "physical_observation_after_s", "unattributed_inside_service_s",
]
metrics = [metric for metric in metrics if any(metric in row for row in rows)]


def summarize_rows(selected: list[dict]) -> dict:
    return {
        "creates": len(selected),
        "metrics": {
            metric: summary([float(row[metric]) for row in selected if metric in row])
            for metric in metrics
        },
    }


by_role = {
    role: summarize_rows([row for row in rows if row["role"] == role])
    for role in sorted({row["role"] for row in rows})
}
by_minute: dict[int, list[dict]] = defaultdict(list)
for row in rows:
    by_minute[int(row["elapsed_s"] // 60)].append(row)

slowest = sorted(rows, key=lambda row: row["service_seconds"], reverse=True)[:20]
pairs: dict[str, dict[str, dict]] = defaultdict(dict)
for row in rows:
    if row.get("session_id"):
        pairs[str(row["session_id"])][str(row["role"])] = row
pair_rows = []
for session_id, roles in pairs.items():
    if "tool" not in roles or "runtime" not in roles:
        continue
    tool = roles["tool"]
    runtime = roles["runtime"]
    pair_rows.append({
        "session_id": session_id,
        "between_tool_and_runtime_s": max(
            0.0, runtime["started_unix_s"] - tool["completed_unix_s"]
        ),
        "pair_create_service_s": max(
            0.0, runtime["completed_unix_s"] - tool["started_unix_s"]
        ),
    })
result = {
    "schema_version": 1,
    "event_path": str(event_path),
    "event_count": event_count,
    "instrumented_create_count": len(rows),
    "overall": summarize_rows(rows),
    "by_role": by_role,
    "by_minute": {
        str(minute): summarize_rows(selected)
        for minute, selected in sorted(by_minute.items())
    },
    "first_50": summarize_rows(rows[:50]),
    "last_50": summarize_rows(rows[-50:]),
    "pair_stages": {
        "pairs": len(pair_rows),
        "between_tool_and_runtime_s": summary([
            row["between_tool_and_runtime_s"] for row in pair_rows
        ]),
        "pair_create_service_s": summary([
            row["pair_create_service_s"] for row in pair_rows
        ]),
    },
    "slowest_20": slowest,
}
print(json.dumps(result, indent=2, sort_keys=True))
