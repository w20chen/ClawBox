#!/usr/bin/env python3
"""Extract throughput and lifecycle timings from a standalone study."""
from __future__ import annotations

import json
import sys
import statistics
from pathlib import Path


def main(root: Path) -> None:
    print("| Policy | c | Status | steps/s | JCT p50 (s) | JCT p95 (s) | create p50 (s) | destroy p50 (s) | pauses | restores | checkpoint p50 (s) | restore-ready p50 (s) | first-tool p50 (s) | dirty bytes p50 | transferred bytes p50 | allocated bytes p50 |")
    print("|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for path in sorted((root / "arms").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        arm, perf, check = row["arm"], row.get("performance", {}), row.get("correctness", {})
        duration = float(perf.get("duration_seconds") or 0)
        steps = int(perf.get("model_steps", 0) or 0) + int(perf.get("tool_steps", 0) or 0)
        event_rows = []
        event_path = Path(str(row.get("artifacts", {}).get("events", "")))
        if event_path.is_file():
            for line in event_path.read_text(encoding="utf-8").splitlines():
                try:
                    event_rows.append(json.loads(line))
                except ValueError:
                    pass
        def event_p50(event: str) -> str:
            values = [float(item["service_seconds"]) for item in event_rows
                      if item.get("event") == event and item.get("service_seconds") is not None]
            return "n/a" if not values else f"{statistics.median(values):.4f}"
        def timing_p50(operation: str) -> str:
            values = []
            for item in event_rows:
                timing = item.get("lifecycle_timing") or {}
                if timing.get("operation") == operation and timing.get("service_seconds") is not None:
                    values.append(float(timing["service_seconds"]))
            return "n/a" if not values else f"{statistics.median(values):.4f}"
        def field_p50(event: str, field: str) -> str:
            values = [float(item[field]) for item in event_rows
                      if item.get("event") == event and item.get(field) is not None]
            return "n/a" if not values else f"{statistics.median(values):.4f}"
        # Arm files carry aggregate timing fields; missing values stay n/a.
        def val(key: str) -> str:
            value = perf.get(key)
            return "n/a" if value is None else f"{float(value):.4f}"
        print("| " + " | ".join([
            str(arm["policy"]["name"]), str(arm["concurrency"]), str(row.get("status")),
            f"{steps / duration:.4f}" if duration > 0 else "n/a",
            val("jct_p50_seconds"), val("jct_p95_seconds"), event_p50("sandbox_created"),
            event_p50("sandbox_destroyed"), str(perf.get("pause_count", 0)),
            str(perf.get("resume_count", 0)), timing_p50("checkpoint"),
            timing_p50("restore"), field_p50("first_tool_after_restore", "first_tool_after_restore_seconds"),
            val("dirty_bytes_p50"), val("transferred_bytes_p50"), val("allocated_bytes_p50"),
        ]) + " |")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: report-tiered-study.py STUDY-DIRECTORY")
    main(Path(sys.argv[1]))
