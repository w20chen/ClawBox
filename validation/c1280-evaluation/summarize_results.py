#!/usr/bin/env python3
"""Print the result fields used by the c1280 tracking plan."""
import json
import sys

for path in sys.argv[1:]:
    result = json.load(open(path, encoding="utf-8"))
    print(path)
    for item in result["arms"]:
        arm = item["arm"]
        correctness = item["correctness"]
        performance = item["performance"]
        memory = item["memory"]
        cpu = performance.get("experiment_cpu") or {}
        print({
            "policy": arm["policy"]["name"],
            "concurrency": arm["concurrency"],
            "status": item["status"],
            "failure_category": item["failure_category"],
            "failure": correctness["failure"],
            "completed": correctness.get("completed_sessions"),
            "failed": correctness.get("failed_sessions"),
            "cleanup": correctness.get("cleanup_verified"),
            "duration_s": performance.get("duration_seconds"),
            "agents_per_minute": performance.get("agents_per_minute"),
            "jct_p95_s": performance.get("jct_p95_seconds"),
            "blocked_admission_s": performance.get("blocked_admission_seconds"),
            "cpu_mean_cores": cpu.get("mean_cores"),
            "cpu_peak_cores": cpu.get("peak_interval_cores"),
            "local_peak_gib": ((memory.get("peak_local_tier_bytes") or 0) / 1024**3),
            "host_peak_delta_gib": ((memory.get("host_peak_used_delta_bytes") or 0) / 1024**3),
            "high_crossings": performance.get("local_high_crossings"),
            "oom_kills": correctness.get("experiment_cgroup_oom_kill_events"),
        })
