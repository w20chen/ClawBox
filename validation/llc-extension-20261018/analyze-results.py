#!/usr/bin/env python3
"""Summarize matched C0/C2 screening runs copied from Kunpeng."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def arm_record(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    arm = raw["arm"]
    performance = raw["performance"]
    correctness = raw["correctness"]
    topology = performance.get("topology_cpu_placement") or {}
    cpu = performance.get("experiment_cpu") or {}
    return {
        "arm_id": arm["arm_id"],
        "policy": arm["policy"]["name"],
        "mode": arm["execution"]["topology_cpu_placement"],
        "status": raw["status"],
        "completed_sessions": correctness["completed_sessions"],
        "failed_sessions": correctness["failed_sessions"],
        "validation_passed": correctness["validation_passed"],
        "cleanup_verified": correctness["cleanup_verified"],
        "success_rate": performance["success_rate"],
        "agents_per_minute": performance["agents_per_minute"],
        "workload_window_seconds": performance["workload_window_seconds"],
        "duration_seconds": performance["duration_seconds"],
        "jct_mean_seconds": performance["jct_mean_seconds"],
        "jct_p95_seconds": performance["jct_p95_seconds"],
        "steps_per_minute": performance["steps_per_minute"],
        "tool_latency_mean_seconds": performance["tool_latency_mean_seconds"],
        "tool_latency_p95_seconds": performance["tool_latency_p95_seconds"],
        "experiment_cpu_mean_cores": cpu.get("mean_cores"),
        "experiment_cpu_peak_interval_cores": cpu.get("peak_interval_cores"),
        "topology_assignments": topology.get("assignments", 0),
        "topology_expansions": topology.get("expanded", 0),
    }


def percent_change(new: float, old: float) -> float:
    return 100.0 * (new / old - 1.0)


def summarize_run(root: Path) -> dict:
    records = [arm_record(path) for path in sorted((root / "arms").glob("*.json"))]
    by_mode = {record["mode"]: record for record in records}
    result = {"run_id": root.name, "arms": records}
    if "off" in by_mode and "cluster_adaptive" in by_mode:
        c0, c2 = by_mode["off"], by_mode["cluster_adaptive"]
        result["c2_vs_c0"] = {
            "agents_per_minute_change_percent": percent_change(
                c2["agents_per_minute"], c0["agents_per_minute"]
            ),
            "workload_window_reduction_percent": -percent_change(
                c2["workload_window_seconds"], c0["workload_window_seconds"]
            ),
            "jct_mean_reduction_percent": -percent_change(
                c2["jct_mean_seconds"], c0["jct_mean_seconds"]
            ),
            "jct_p95_reduction_percent": -percent_change(
                c2["jct_p95_seconds"], c0["jct_p95_seconds"]
            ),
            "steps_per_minute_change_percent": percent_change(
                c2["steps_per_minute"], c0["steps_per_minute"]
            ),
            "tool_latency_mean_reduction_percent": -percent_change(
                c2["tool_latency_mean_seconds"], c0["tool_latency_mean_seconds"]
            ),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_roots", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    runs = [summarize_run(root) for root in args.run_roots]
    effects = [run["c2_vs_c0"] for run in runs if "c2_vs_c0" in run]
    aggregate = {}
    if effects:
        for key in effects[0]:
            values = [effect[key] for effect in effects]
            aggregate[key] = {
                "mean": statistics.fmean(values),
                "min": min(values),
                "max": max(values),
                "values": values,
            }
    result = {"runs": runs, "paired_effects": aggregate}
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
