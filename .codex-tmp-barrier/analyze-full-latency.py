#!/usr/bin/env python3
"""Stream a ClawBox event JSONL and summarize queue/control-plane latency."""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def stats(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "mean": sum(values) / len(values) if values else None,
        "p50": quantile(values, 0.50),
        "p90": quantile(values, 0.90),
        "p95": quantile(values, 0.95),
        "p99": quantile(values, 0.99),
        "max": max(values) if values else None,
    }


def main(path: str) -> None:
    counts: Counter[str] = Counter()
    event_keys: dict[str, set[str]] = defaultdict(set)
    numeric: dict[tuple[str, str], list[float]] = defaultdict(list)
    execution_times: dict[str, dict[str, float]] = defaultdict(dict)
    execution_scope: dict[str, str] = {}
    execution_operation: dict[str, str] = {}
    sessions_by_event: dict[str, set[str]] = defaultdict(set)
    memory_cpu: list[float] = []
    memory_local: list[float] = []
    memory_shared: list[float] = []
    session_spans: dict[str, list[float]] = defaultdict(list)
    startup_stages: dict[str, list[float]] = defaultdict(list)
    control_stages: dict[str, list[float]] = defaultdict(list)
    queue_snapshot_max: dict[str, float] = {}
    queue_snapshot_numeric: dict[str, list[float]] = defaultdict(list)
    queue_snapshot_samples = 0
    memory_decision_actions: Counter[str] = Counter()
    memory_decision_reasons: Counter[str] = Counter()
    wait_with_borrow_candidate = 0
    wait_with_borrow_candidate_and_capacity = 0
    wait_with_borrow_candidate_and_capacity_reasons: Counter[str] = Counter()
    memory_blocked_with_pool_headroom_samples = 0
    memory_blocked_samples = 0
    vm_create_backlog_samples = 0
    tool_start_backlog_samples = 0
    tool_start_backlog_low_cpu_samples = 0

    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            event = str(row.get("event") or "")
            if not event:
                continue
            counts[event] += 1
            event_keys[event].update(row)
            session_id = row.get("session_id")
            if session_id:
                sessions_by_event[event].add(str(session_id))
            for key, value in row.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if key.endswith("_seconds") or key in {
                        "wait_seconds", "held_seconds", "service_seconds",
                        "control_total_seconds", "admission_blocked_seconds",
                    }:
                        numeric[event, key].append(float(value))
            execution_id = row.get("execution_id")
            if execution_id and row.get("wall_time_ns") is not None:
                execution_id = str(execution_id)
                execution_times[execution_id][event] = int(row["wall_time_ns"]) / 1e9
                if row.get("execution_scope"):
                    execution_scope[execution_id] = str(row["execution_scope"])
                if row.get("operation"):
                    execution_operation[execution_id] = str(row["operation"])
            if event == "memory_sample":
                cpu = (row.get("cgroup_cpu") or {}).get("average_cores")
                if isinstance(cpu, (int, float)):
                    memory_cpu.append(float(cpu))
                for key, target in (
                    ("local_used_bytes", memory_local),
                    ("shared_live_used_bytes", memory_shared),
                ):
                    value = row.get(key)
                    if isinstance(value, (int, float)):
                        target.append(float(value))
                queue_snapshot = row.get("queue_activity_snapshot") or {}
                if isinstance(queue_snapshot, dict):
                    queue_snapshot_samples += 1
                    for key, value in queue_snapshot.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            queue_snapshot_numeric[key].append(float(value))
                            queue_snapshot_max[key] = max(
                                queue_snapshot_max.get(key, float("-inf")), float(value),
                            )
            elif event == "session_timing":
                for span in row.get("time_spans") or []:
                    name = span.get("name")
                    duration = span.get("duration_seconds")
                    if name and isinstance(duration, (int, float)):
                        session_spans[str(name)].append(float(duration))
            elif event == "openclaw_startup_stage":
                stage = row.get("stage")
                duration = row.get("duration_seconds")
                if stage and isinstance(duration, (int, float)):
                    startup_stages[str(stage)].append(float(duration))
            elif event == "tool_admitted":
                for stage, duration in (row.get("control_stage_seconds") or {}).items():
                    if isinstance(duration, (int, float)):
                        control_stages[str(stage)].append(float(duration))
            elif event == "memory_scheduling_decision":
                action = str(row.get("action") or "unknown")
                reason = str(row.get("reason") or "unknown")
                memory_decision_actions[action] += 1
                memory_decision_reasons[f"{action}:{reason}"] += 1
                borrow = (row.get("candidates") or {}).get("borrow")
                pool = row.get("pool") or {}
                if action == "wait" and isinstance(borrow, dict):
                    wait_with_borrow_candidate += 1
                    required = int(borrow.get("configured_bytes") or 0)
                    borrowed = int(pool.get("borrowed_bytes") or 0)
                    reserved = int(pool.get("reserved_bytes") or 0)
                    committed = int(pool.get("committed_bytes") or 0)
                    borrow_capacity = int(pool.get("borrow_capacity_bytes") or 0)
                    capacity = int(pool.get("capacity_bytes") or 0)
                    has_capacity = (
                        required > 0
                        and borrowed + required <= borrow_capacity
                        and borrowed + reserved + committed + required <= capacity
                    )
                    if has_capacity:
                        wait_with_borrow_candidate_and_capacity += 1
                        wait_with_borrow_candidate_and_capacity_reasons[reason] += 1

            if event == "memory_sample":
                activity = row.get("queue_activity_snapshot") or {}
                admission = row.get("admission_queue_snapshot") or {}
                memory_blocked = (
                    int(admission.get("admission_queue_depth") or 0) > 0
                    or int(activity.get("tool_admission_waiting_operations") or 0) > 0
                    or int(activity.get("tool_admission_waiting_tasks") or 0) > 0
                )
                if memory_blocked:
                    memory_blocked_samples += 1
                    borrowed = int(row.get("shared_borrow_reserved_bytes") or 0)
                    reserved = int(row.get("warm_reserved_bytes") or 0)
                    committed = int(row.get("warm_committed_bytes") or 0)
                    # The formal spec reserves 50 GiB for borrowing within a
                    # 100 GiB shared pool. The decision-event calculation above
                    # remains authoritative because it uses the logged limits
                    # and a concrete candidate size.
                    if borrowed < 50 * 2**30 and borrowed + reserved + committed < 100 * 2**30:
                        memory_blocked_with_pool_headroom_samples += 1
                if int(activity.get("vm_create_waiting_sessions") or 0) > 0:
                    vm_create_backlog_samples += 1
                if int(activity.get("tool_admitted_waiting_start_operations") or 0) > 0:
                    tool_start_backlog_samples += 1
                    cpu = (row.get("cgroup_cpu") or {}).get("utilization_fraction")
                    if isinstance(cpu, (int, float)) and float(cpu) < 0.8:
                        tool_start_backlog_low_cpu_samples += 1

    transitions: dict[str, list[float]] = defaultdict(list)
    pairs = {
        "queued_to_admitted": ("tool_admission_queued", "tool_admitted"),
        "admitted_to_execution_start": ("tool_admitted", "tool_execution_progress:start"),
        "execution_start_to_end": ("tool_execution_progress:start", "tool_execution_progress:end"),
        "admitted_to_completed": ("tool_admitted", "tool_completed"),
    }
    # Store progress phases under synthetic names; older logs use one event name.
    execution_times.clear()
    execution_scope.clear()
    execution_operation.clear()
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            execution_id = row.get("execution_id")
            wall_ns = row.get("wall_time_ns")
            if not execution_id or wall_ns is None:
                continue
            event = str(row.get("event") or "")
            if event == "tool_execution_progress":
                event += ":" + str(row.get("phase") or "")
            execution_id = str(execution_id)
            execution_times[execution_id][event] = int(wall_ns) / 1e9
            if row.get("execution_scope"):
                execution_scope[execution_id] = str(row["execution_scope"])
            if row.get("operation"):
                execution_operation[execution_id] = str(row["operation"])
    for execution_id, timeline in execution_times.items():
        for name, (start, end) in pairs.items():
            if start in timeline and end in timeline and timeline[end] >= timeline[start]:
                elapsed = timeline[end] - timeline[start]
                transitions[name].append(elapsed)
                group = (
                    execution_scope.get(execution_id, "unknown")
                    + ":"
                    + execution_operation.get(execution_id, "unknown")
                )
                transitions[f"{name}|{group}"].append(elapsed)

    output = {
        "event_counts": counts.most_common(),
        "session_coverage": {
            event: len(sessions)
            for event, sessions in sorted(sessions_by_event.items())
            if event in {
                "sandbox_create_gate_wait_started", "sandbox_create_gate_acquired",
                "sandbox_create_gate_released", "session_started", "session_finished",
                "tool_admission_queued", "tool_admitted", "tool_completed",
            }
        },
        "numeric_event_fields": {
            f"{event}.{key}": stats(values)
            for (event, key), values in sorted(numeric.items())
            if values
        },
        "execution_transitions": {
            name: stats(values) for name, values in transitions.items()
        },
        "session_spans": {
            name: stats(values) for name, values in sorted(session_spans.items())
        },
        "startup_stages": {
            name: stats(values) for name, values in sorted(startup_stages.items())
        },
        "control_stages": {
            name: stats(values) for name, values in sorted(control_stages.items())
        },
        "queue_activity": {
            "samples": queue_snapshot_samples,
            "max": dict(sorted(queue_snapshot_max.items())),
            "field_statistics": {
                key: stats(values)
                for key, values in sorted(queue_snapshot_numeric.items())
            },
        },
        "resource_samples": {
            "cpu_average_cores": stats(memory_cpu),
            "local_used_gib": stats([value / 2**30 for value in memory_local]),
            "shared_live_used_gib": stats([value / 2**30 for value in memory_shared]),
        },
        "shared_pool_utilization_diagnosis": {
            "memory_scheduling_actions": dict(memory_decision_actions),
            "memory_scheduling_action_reasons": dict(memory_decision_reasons),
            "wait_with_borrow_candidate": wait_with_borrow_candidate,
            "wait_with_borrow_candidate_and_logged_capacity": (
                wait_with_borrow_candidate_and_capacity
            ),
            "wait_with_borrow_candidate_and_logged_capacity_reasons": dict(
                wait_with_borrow_candidate_and_capacity_reasons
            ),
            "memory_blocked_samples": memory_blocked_samples,
            "memory_blocked_with_configured_pool_headroom_samples": (
                memory_blocked_with_pool_headroom_samples
            ),
            "vm_create_backlog_samples_not_itself_shared_pool_eligible": (
                vm_create_backlog_samples
            ),
            "tool_admitted_waiting_start_samples": tool_start_backlog_samples,
            "tool_admitted_waiting_start_below_80pct_cpu_samples": (
                tool_start_backlog_low_cpu_samples
            ),
            "interpretation": (
                "A wait with a concrete borrow candidate and logged capacity is "
                "an actionable shared-pool utilization defect. Memory-blocked "
                "samples with headroom are suspects until correlated with a "
                "decision. VM-create backlog alone is a create/control-plane "
                "bottleneck, not proof that borrowing should occur."
            ),
        },
        "selected_event_keys": {
            event: sorted(event_keys[event])
            for event in (
                "sandbox_create_gate_acquired", "sandbox_create_gate_released",
                "tool_admission_queued", "tool_admitted", "tool_completed",
                "tool_execution_progress", "memory_sample",
            )
            if event in event_keys
        },
    }
    json.dump(output, sys.stdout, indent=2, sort_keys=True)
    print()


if __name__ == "__main__":
    main(sys.argv[1])
