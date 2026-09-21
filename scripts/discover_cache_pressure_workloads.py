"""Discover CPU-active, low-IPC, cache-pressure workloads from trace PMU data."""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path


CALLS = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\placement-first-pass\calls.json")
OUT = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\llc-placement-discovery-20260914\cache_pressure_candidates.csv")

EXCLUDE = re.compile(
    r"(?:git\s+(?:clone|pull|fetch|lfs\s+(?:pull|fetch))|\b(?:curl|wget)\b.*https?://|"
    r"\b(?:apt|apt-get|pip|pip3|npm|yarn)\s+(?:install|download|update)|"
    r"\b(?:sleep|poll|wait)\b|timeout\s+(?:20|30|45|60|120|180|290|300)\b)", re.I | re.S
)
PYTEST_OR_LOCAL = re.compile(r"(?:pytest|unittest|flake8|mypy|ruff|cmake|make\b|gcc|g\+\+|clang|python\s+-m\s+compileall|parse_|parser|static)", re.I)


def trace_pmu(path: Path, span_id: str) -> dict | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("record_type") != "span_end" or row.get("span_id") != span_id:
            continue
        pmu = (row.get("resources") or {}).get("pmu") or {}
        if (pmu.get("coverage") or {}).get("status") != "reliable":
            return None
        events = pmu.get("events") or {}
        keys = {"llc_read_accesses": "read", "llc_read_misses": "miss", "cycles": "cycles", "instructions": "instructions"}
        values = {}
        running = []
        for source, dest in keys.items():
            event = events.get(source) or {}
            raw = event.get("raw_count")
            if not isinstance(raw, (int, float)):
                return None
            values[dest] = raw
            running.append(event.get("time_running_ns", 0))
        active_s = min(running) / 1e9
        elapsed_s = max(0.001, float(pmu.get("ended_at", 0.0)) - float(pmu.get("started_at", 0.0)))
        values["pmu_active_s"] = active_s
        values["pmu_elapsed_s"] = elapsed_s
        values["pmu_running_ratio"] = active_s / elapsed_s
        values["llc_read_M_per_CPU_s"] = values["read"] / active_s / 1e6
        values["llc_miss_M_per_CPU_s"] = values["miss"] / active_s / 1e6
        values["H_M_per_CPU_s"] = (values["read"] - values["miss"]) / active_s / 1e6
        values["llc_miss_rate"] = values["miss"] / values["read"] if values["read"] else math.nan
        values["llc_mpki"] = values["miss"] * 1000 / values["instructions"] if values["instructions"] else math.nan
        values["ipc"] = values["instructions"] / values["cycles"] if values["cycles"] else math.nan
        return values
    return None


def main() -> None:
    calls = json.loads(CALLS.read_text(encoding="utf-8"))
    rows = []
    for call in calls:
        duration = float(call.get("duration_s") or 0.0)
        if duration < 10.0 or call.get("pmu_coverage") != "reliable":
            continue
        command = call.get("command") or call.get("requested_args", {}).get("command") or ""
        pmu = trace_pmu(Path(call["trace_path"]), call["span_id"])
        if not pmu:
            continue
        busy = float(call.get("busy_ratio") or 0.0)
        cpu_time = call.get("cpu_time_s")
        cpu_ratio = float(cpu_time) / duration if isinstance(cpu_time, (int, float)) else math.nan
        cpu_active = max(busy, cpu_ratio if not math.isnan(cpu_ratio) else 0.0)
        excluded = EXCLUDE.search(command) is not None
        cpu_bound_shape = PYTEST_OR_LOCAL.search(command) is not None
        # The thresholds are deliberately explicit and independent of H:
        # keep CPU-active, high-MPKI, low-IPC, medium/high-miss workloads.
        pressure_eligible = (
            not excluded
            and cpu_active >= 0.80
            and pmu["llc_mpki"] >= 0.25
            and pmu["ipc"] <= 1.25
            and pmu["llc_miss_rate"] >= 0.25
        )
        rows.append({
            "task_id": call["task_id"],
            "span_id": call["span_id"],
            "trace_id": call.get("trace_id", ""),
            "sequence_no": call.get("sequence_no", ""),
            "suite": call.get("suite"),
            "repo": call.get("repo"),
            "duration_s": duration,
            "cpu_time_s": cpu_time,
            "cpu_time_ratio": cpu_ratio,
            "busy_ratio": busy,
            "cpu_active_ratio": cpu_active,
            "pmu_active_s": pmu["pmu_active_s"],
            "pmu_elapsed_s": pmu["pmu_elapsed_s"],
            "pmu_running_ratio": pmu["pmu_running_ratio"],
            "llc_read_M_per_CPU_s": pmu["llc_read_M_per_CPU_s"],
            "llc_miss_M_per_CPU_s": pmu["llc_miss_M_per_CPU_s"],
            "H_M_per_CPU_s": pmu["H_M_per_CPU_s"],
            "llc_miss_rate": pmu["llc_miss_rate"],
            "llc_mpki": pmu["llc_mpki"],
            "ipc": pmu["ipc"],
            "local_cpu_tool_shape": cpu_bound_shape,
            "excluded_external_or_wait": excluded,
            "pressure_eligible": pressure_eligible,
            "command": command,
            "trace_path": str(call["trace_path"]),
        })

    rows.sort(key=lambda x: (
        not x["pressure_eligible"],
        not x["local_cpu_tool_shape"],
        -x["llc_mpki"],
        -x["llc_miss_rate"],
        x["ipc"],
        -x["cpu_active_ratio"],
        -x["duration_s"],
    ))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    eligible = [x for x in rows if x["pressure_eligible"]]
    print(f"pmu_reliable_duration_ge_10={len(rows)} pressure_eligible={len(eligible)}")
    for row in eligible[:60]:
        print(
            row["task_id"], row["span_id"], row["repo"],
            f"dur={row['duration_s']:.1f}", f"cpu_active={row['cpu_active_ratio']:.2f}",
            f"MPKI={row['llc_mpki']:.3f}", f"IPC={row['ipc']:.3f}",
            f"miss_rate={row['llc_miss_rate']:.3f}",
            row["command"][:120].replace("\n", " "),
        )


if __name__ == "__main__":
    main()
