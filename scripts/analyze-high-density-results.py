#!/usr/bin/env python3
"""Compare resident and snapshot high-density replay reports."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def resources(path: Path) -> dict[str, float | int]:
    samples = json.loads(path.read_text(encoding="utf-8"))
    rss = [row["rss_bytes"] for row in samples]
    resident = [row["resident_vms"] for row in samples]
    numa = [row["numa_memory_used_bytes"] for row in samples
            if row["numa_memory_used_bytes"] is not None]
    ordered = sorted(rss)
    p95 = ordered[int((len(ordered) - 1) * 0.95)]
    return {
        "samples": len(samples),
        "mean_firecracker_rss_mib": statistics.fmean(rss) / 2**20,
        "median_firecracker_rss_mib": statistics.median(rss) / 2**20,
        "p95_firecracker_rss_mib": p95 / 2**20,
        "peak_firecracker_rss_mib": max(rss) / 2**20,
        "mean_resident_vms": statistics.fmean(resident),
        "median_resident_vms": statistics.median(resident),
        "zero_resident_fraction": sum(value == 0 for value in resident) / len(resident),
        "mean_numa_used_gib": statistics.fmean(numa) / 2**30,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    for mode in ("resident", "snapshot"):
        parser.add_argument(f"--{mode}-summary", required=True, type=Path)
        parser.add_argument(f"--{mode}-rss", required=True, type=Path)
    args = parser.parse_args()
    resident = json.loads(args.resident_summary.read_text(encoding="utf-8"))
    snapshot = json.loads(args.snapshot_summary.read_text(encoding="utf-8"))
    resident_resources = resources(args.resident_rss)
    snapshot_resources = resources(args.snapshot_rss)
    report = {
        "resident": {"summary": resident, "resources": resident_resources},
        "snapshot": {"summary": snapshot, "resources": snapshot_resources},
        "comparison": {
            "wall_time_reduction_fraction": 1 - snapshot["wall_s"] / resident["wall_s"],
            "throughput_increase_fraction": (
                snapshot["throughput_sessions_per_hour"]
                / resident["throughput_sessions_per_hour"] - 1
            ),
            "mean_rss_reduction_fraction": 1 - (
                snapshot_resources["mean_firecracker_rss_mib"]
                / resident_resources["mean_firecracker_rss_mib"]
            ),
            "rss_time_reduction_fraction": 1 - (
                snapshot_resources["mean_firecracker_rss_mib"] * snapshot["wall_s"]
                / (resident_resources["mean_firecracker_rss_mib"] * resident["wall_s"])
            ),
            "resident_vm_time_reduction_fraction": 1 - (
                snapshot_resources["mean_resident_vms"] * snapshot["wall_s"]
                / (resident_resources["mean_resident_vms"] * resident["wall_s"])
            ),
        },
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
