#!/usr/bin/env python3
"""Print the result fields used by the c1280 tracking plan."""
import json
import sys
from collections import Counter
from pathlib import Path


def event_evidence(item):
    path = Path((item.get("artifacts") or {}).get("events", ""))
    if not path.is_file():
        return {}
    counts = Counter()
    pause_roles = Counter()
    maxima = Counter()
    failures = Counter()
    for line in path.open(encoding="utf-8"):
        row = json.loads(line)
        event = str(row.get("event", "unknown"))
        counts[event] += 1
        if event in {"sandbox_paused", "sandbox_restored"}:
            pause_roles[f"{event}:{row.get('role')}"] += 1
        if event == "session_failed":
            failures[str(row.get("error_type") or row.get("error"))] += 1
        if event == "memory_sample":
            for key in ("warm_allocated_bytes", "warm_reserved_bytes",
                        "warm_committed_bytes", "shared_live_used_bytes"):
                maxima[key] = max(maxima[key], int(row.get(key) or 0))
            maxima["warm_physical_plus_shared_bytes"] = max(
                maxima["warm_physical_plus_shared_bytes"],
                int(row.get("warm_allocated_bytes") or 0)
                + int(row.get("shared_live_used_bytes") or 0),
            )
            maxima["warm_accounted_plus_shared_bytes"] = max(
                maxima["warm_accounted_plus_shared_bytes"],
                int(row.get("warm_reserved_bytes") or 0)
                + int(row.get("warm_committed_bytes") or 0)
                + int(row.get("shared_live_used_bytes") or 0),
            )
    return {
        "event_counts": dict(counts),
        "paired_roles": dict(pause_roles),
        "session_failure_types": dict(failures),
        "event_memory_peaks_gib": {
            key: value / 1024**3 for key, value in maxima.items()
        },
    }


def admission_wait_seconds(performance):
    admission = performance.get("admission_control") or {}
    if admission.get("scope") == "per_compute_node":
        return sum(float(node.get("wait_total_seconds") or 0)
                   for node in admission.get("nodes", {}).values())
    return admission.get("wait_total_seconds")

for path in sys.argv[1:]:
    result = json.load(open(path, encoding="utf-8"))
    print(path)
    for item in result["arms"]:
        arm = item["arm"]
        correctness = item["correctness"]
        performance = item["performance"]
        memory = item["memory"]
        cpu = performance.get("experiment_cpu") or {}
        runtime = performance.get("runtime_memory_control") or {}
        completed = correctness.get("completed_sessions", 0)
        duration = performance.get("duration_seconds")
        print({
            "policy": arm["policy"]["name"],
            "concurrency": arm["concurrency"],
            "status": item["status"],
            "failure_category": item["failure_category"],
            "failure": correctness["failure"],
            "completed": completed,
            "failed": correctness.get("failed_sessions"),
            "cleanup": correctness.get("cleanup_verified"),
            "duration_s": duration,
            "successful_agents_per_s": (
                completed / duration if duration else None
            ),
            "agents_per_minute": performance.get("agents_per_minute"),
            "success_rate": performance.get("success_rate", (
                correctness.get("completed_sessions", 0) / arm["concurrency"]
                if arm.get("concurrency") else None
            )),
            "jct_p50_s": performance.get("jct_p50_seconds"),
            "jct_p95_s": performance.get("jct_p95_seconds"),
            "jct_p99_s": performance.get("jct_p99_seconds"),
            "offered_p50_s": performance.get("offered_to_completion_p50_seconds"),
            "offered_p95_s": performance.get("offered_to_completion_p95_seconds"),
            "offered_p99_s": performance.get("offered_to_completion_p99_seconds"),
            "blocked_admission_s": performance.get("blocked_admission_seconds"),
            "admission_wait_total_s": admission_wait_seconds(performance),
            "cpu_mean_cores": cpu.get("mean_cores"),
            "cpu_peak_cores": cpu.get("peak_interval_cores"),
            "local_peak_gib": ((memory.get("peak_local_tier_bytes") or 0) / 1024**3),
            "shared_live_peak_gib": ((memory.get("peak_shared_live_bytes") or 0) / 1024**3),
            "shared_total_peak_gib": ((memory.get("peak_shared_total_bytes") or 0) / 1024**3),
            "combined_live_peak_gib": ((memory.get("peak_combined_live_bytes") or 0) / 1024**3),
            "host_peak_delta_gib": ((memory.get("host_peak_used_delta_bytes") or 0) / 1024**3),
            "high_crossings": performance.get("local_high_crossings"),
            "oom_kills": correctness.get("experiment_cgroup_oom_kill_events"),
            "borrow_count": performance.get("shared_memory_borrow_count"),
            "return_count": performance.get("shared_memory_return_count"),
            "pause_count": performance.get("pause_count"),
            "pause_service_s": performance.get("pause_service_seconds"),
            "resume_count": performance.get("resume_count"),
            "resume_service_s": performance.get("resume_service_seconds"),
            "runtime_memory_protections": runtime.get("memory_protections"),
            "runtime_cpu_boosts": runtime.get("cpu_boosts"),
            "runtime_rollbacks": runtime.get("rollbacks"),
            "telemetry_loss": correctness.get("native_tool_telemetry_loss_total"),
            **event_evidence(item),
        })
