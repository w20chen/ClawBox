"""Rank PMU-reliable trace calls for LLC-placement follow-up experiments."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path


CALLS = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\placement-first-pass\calls.json")
OUT = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\llc-placement-discovery-20260914\discovered_candidates.csv")
EXCLUDE = re.compile(
    r"(?:git\s+clone|\b(?:curl|wget)\b.*https?://|\b(?:apt|apt-get|pip|pip3|npm|yarn)\s+(?:install|download)|\b(?:sleep|poll|wait)\b|timeout\s+(?:45|60|120|180)\b|git\s+fetch|git\s+pull)",
    re.I | re.S,
)


def pmu_from_trace(path: Path, span_id: str) -> dict | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("record_type") != "span_end" or row.get("span_id") != span_id:
            continue
        pmu = (row.get("resources") or {}).get("pmu") or {}
        if (pmu.get("coverage") or {}).get("status") != "reliable":
            return None
        events = pmu.get("events") or {}
        read = (events.get("llc_read_accesses") or {}).get("raw_count")
        miss = (events.get("llc_read_misses") or {}).get("raw_count")
        cycles = (events.get("cycles") or {}).get("raw_count")
        instructions = (events.get("instructions") or {}).get("raw_count")
        elapsed = float(pmu.get("started_at", 0.0))
        end = float(pmu.get("ended_at", 0.0))
        elapsed = max(0.001, end - elapsed)
        active = min(
            (events.get(name) or {}).get("time_running_ns", 0) for name in ("llc_read_accesses", "llc_read_misses", "cycles", "instructions")
        ) / 1e9
        if not all(isinstance(x, (int, float)) for x in (read, miss, cycles, instructions)):
            return None
        return {
            "llc_read_M_per_CPU_s": read / active / 1e6,
            "llc_miss_M_per_CPU_s": miss / active / 1e6,
            "H_M_per_CPU_s": (read - miss) / active / 1e6,
            "llc_miss_rate": miss / read if read else None,
            "ipc": instructions / cycles if cycles else None,
            "pmu_active_s": active,
            "pmu_elapsed_s": elapsed,
            "running_ratio": active / elapsed,
        }
    return None


def main() -> None:
    calls = json.loads(CALLS.read_text(encoding="utf-8"))
    rows = []
    for call in calls:
        if call.get("pmu_coverage") != "reliable" or call.get("duration_s", 0.0) < 2.0:
            continue
        if call.get("busy_ratio", 0.0) < 0.5:
            continue
        command = call.get("command") or call.get("requested_args", {}).get("command") or ""
        if EXCLUDE.search(command):
            continue
        trace_path = Path(call["trace_path"])
        pmu = pmu_from_trace(trace_path, call["span_id"])
        if not pmu:
            continue
        rows.append(
            {
                "task_id": call["task_id"],
                "span_id": call["span_id"],
                "suite": call.get("suite"),
                "repo": call.get("repo"),
                "duration_s": call["duration_s"],
                "busy_ratio": call.get("busy_ratio"),
                "cpu_time_s": call.get("cpu_time_s"),
                **pmu,
                "command": command,
                "trace_path": str(trace_path),
            }
        )
    rows.sort(key=lambda row: (-row["H_M_per_CPU_s"], -row["llc_read_M_per_CPU_s"], -row["duration_s"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"eligible={len(rows)}")
    for row in rows[:50]:
        print(row["task_id"], row["span_id"], f"duration={row['duration_s']:.3f}", f"H={row['H_M_per_CPU_s']:.3f}", f"read={row['llc_read_M_per_CPU_s']:.3f}", row["command"][:100].replace("\n", "\\n"))


if __name__ == "__main__":
    main()
