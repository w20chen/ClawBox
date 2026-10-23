#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import statistics
import sys
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
policy_root = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
operations: dict[str, dict] = {}
active_ids: set[str] = set()
waiting_ids: set[str] = set()
with event_path.open(encoding="utf-8") as stream:
    for line in stream:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        name = event.get("event")
        if not isinstance(name, str) or not name.startswith("dependency_install_gate_"):
            continue
        execution_id = str(event.get("execution_id") or "")
        if not execution_id:
            continue
        row = operations.setdefault(execution_id, {
            "execution_id": execution_id,
            "session_id": event.get("session_id"),
        })
        if name == "dependency_install_gate_wait_started":
            row["wait_started_unix_s"] = int(event["wall_time_ns"]) / 1e9
            row["waiting_ahead_at_start"] = len(waiting_ids)
            waiting_ids.add(execution_id)
        elif name == "dependency_install_gate_acquired":
            row["wait_seconds"] = float(event["wait_seconds"])
            row["acquired_unix_s"] = int(event["wall_time_ns"]) / 1e9
            waiting_ids.discard(execution_id)
            active_ids.add(execution_id)
            row["active_on_acquire"] = len(active_ids)
            row["waiting_when_acquired"] = len(waiting_ids)
        elif name == "dependency_install_gate_released":
            row["held_seconds"] = float(event["held_seconds"])
            row["release_reason"] = event.get("reason")
            row["released_unix_s"] = int(event["wall_time_ns"]) / 1e9
            active_ids.discard(execution_id)
        elif name == "dependency_install_gate_wait_timed_out":
            row["timed_out"] = True
            row["wait_seconds"] = float(event["wait_seconds"])
            waiting_ids.discard(execution_id)

if policy_root is not None:
    by_session: dict[str, list[dict]] = {}
    for row in operations.values():
        session_id = str(row.get("session_id") or "")
        if not session_id or session_id in by_session:
            continue
        path = policy_root / f"{session_id}.json"
        if not path.is_file():
            continue
        try:
            by_session[session_id] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
    for execution_id, row in operations.items():
        for item in by_session.get(str(row.get("session_id") or ""), []):
            request = item.get("request") or {}
            if str(request.get("execution_id") or "") != execution_id:
                continue
            completion = item.get("completion") or {}
            row["command"] = request.get("command")
            row["operation"] = request.get("operation")
            row["exit_code"] = completion.get("exit_code")
            row["execution_started_at"] = completion.get("execution_started_at")
            row["execution_completed_at"] = completion.get("execution_completed_at")
            break

rows = list(operations.values())
completed = [row for row in rows if "held_seconds" in row]
active = [row for row in rows if "acquired_unix_s" in row and "held_seconds" not in row]
waiting = [row for row in rows if "acquired_unix_s" not in row and not row.get("timed_out")]
result = {
    "schema_version": 1,
    "event_path": str(event_path),
    "operations": len(rows),
    "completed": len(completed),
    "active_at_eof": len(active),
    "waiting_at_eof": len(waiting),
    "timed_out": sum(bool(row.get("timed_out")) for row in rows),
    "wait_seconds": summary([
        float(row["wait_seconds"]) for row in rows if "wait_seconds" in row
    ]),
    "held_seconds": summary([
        float(row["held_seconds"]) for row in completed
    ]),
    "release_reasons": {
        reason: sum(row.get("release_reason") == reason for row in completed)
        for reason in sorted({str(row.get("release_reason")) for row in completed})
    },
    "held_seconds_by_active_on_acquire": {
        str(active): summary([
            float(row["held_seconds"])
            for row in completed
            if row.get("active_on_acquire") == active
        ])
        for active in sorted({
            int(row["active_on_acquire"])
            for row in completed if "active_on_acquire" in row
        })
    },
    "slowest_completed": sorted(
        completed, key=lambda row: float(row["held_seconds"]), reverse=True,
    )[:20],
    "active": active,
    "waiting": waiting,
}
print(json.dumps(result, indent=2, sort_keys=True))
