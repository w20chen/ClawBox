"""Summarize CPU and Tool execution evidence from a ClawBox event JSONL stream.

Usage with the retained archive:
    tar -xOf supplement-audit-20261018.tar.zst MEMBER.jsonl |
        python analyze-cpu.py --label RUN
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sys


def quantiles(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    result = {}
    for name, fraction in (("p50", 0.50), ("p90", 0.90), ("p95", 0.95), ("max", 1.0)):
        result[name] = ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]
    return result


parser = argparse.ArgumentParser()
parser.add_argument("--label", required=True)
parser.add_argument("--seconds", type=float, help="Stop this many seconds after the first event")
args = parser.parse_args()

cpu_cores = []
running_vms = []
node_cpu = collections.defaultdict(list)
node_scheduling_pressure = collections.defaultdict(list)
scope_by_execution = {}
start_by_execution = {}
durations = collections.defaultdict(list)
completed_by_scope = collections.Counter()
completed_app_by_session = collections.Counter()
event_counts = collections.Counter()
first_cpu = None
last_cpu = None
origin = None

for line in sys.stdin:
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        continue
    timestamp = int(event.get("wall_time_ns") or 0) / 1e9
    if origin is None:
        origin = timestamp
    if args.seconds is not None and timestamp > origin + args.seconds:
        break
    kind = event.get("event")
    event_counts[kind] += 1
    if kind == "memory_sample":
        cpu = event.get("cgroup_cpu") or {}
        if cpu.get("average_cores") is not None:
            cpu_cores.append(float(cpu["average_cores"]))
        if first_cpu is None:
            first_cpu = cpu
        last_cpu = cpu
        nodes = event.get("compute_pressure") or {}
        running_vms.append(sum(int(node.get("running_vms") or 0) for node in nodes.values()))
        for node_id, node in nodes.items():
            if node.get("cpu_utilization") is not None:
                node_cpu[node_id].append(float(node["cpu_utilization"]))
            if node.get("scheduling_pressure") is not None:
                node_scheduling_pressure[node_id].append(float(node["scheduling_pressure"]))
    elif kind == "tool_admitted":
        scope_by_execution[event.get("execution_id")] = event.get("execution_scope", "unspecified")
    elif kind == "tool_execution_progress" and event.get("phase") == "start":
        start_by_execution[event.get("execution_id")] = int(event["wall_time_ns"]) / 1e9
    elif kind == "tool_completed":
        execution_id = event.get("execution_id")
        scope = scope_by_execution.get(execution_id, "unmatched")
        completed_by_scope[scope] += 1
        if scope == "agent-tool":
            completed_app_by_session[event.get("session_id")] += 1
        if execution_id in start_by_execution:
            durations[scope].append(int(event["wall_time_ns"]) / 1e9 - start_by_execution[execution_id])

cpu_counter_delta = None
if first_cpu and last_cpu:
    cpu_counter_delta = {
        "nr_throttled": int(last_cpu.get("nr_throttled") or 0) - int(first_cpu.get("nr_throttled") or 0),
        "throttled_seconds": (
            int(last_cpu.get("throttled_usec") or 0) - int(first_cpu.get("throttled_usec") or 0)
        ) / 1e6,
    }

report = {
    "label": args.label,
    "requested_window_seconds": args.seconds,
    "memory_sample_count": len(cpu_cores),
    "aggregate_vm_cgroup_average_cores": quantiles(cpu_cores),
    "running_vms_at_samples": quantiles([float(value) for value in running_vms]),
    "node_cpu_utilization_fraction": {
        node: quantiles(values) for node, values in sorted(node_cpu.items())
    },
    "node_scheduling_pressure_fraction": {
        node: quantiles(values) for node, values in sorted(node_scheduling_pressure.items())
    },
    "cpu_throttle_counter_delta": cpu_counter_delta,
    "completed_execution_scopes": dict(completed_by_scope),
    "execution_duration_seconds": {
        scope: quantiles(values) for scope, values in sorted(durations.items())
    },
    "completed_agent_tools_per_session": quantiles(
        [float(value) for value in completed_app_by_session.values()]
    ),
    "sessions_with_completed_agent_tools": len(completed_app_by_session),
    "session_complete_events": event_counts["session_complete"],
    "session_failed_events": event_counts["session_failed"],
}
json.dump(report, sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
