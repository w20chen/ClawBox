"""Flatten placement runner JSONL into audit-friendly trial and summary CSVs."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path


def stat_text(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return out


def trial_row(row: dict) -> dict:
    reps = list(row.get("replicas", {}).values())
    cpu = [stat_text(x.get("cpu_stat_after", "")) for x in reps]
    before = [stat_text(x.get("cpu_stat_before", "")) for x in reps]
    usage_usec = sum(a.get("usage_usec", 0) - b.get("usage_usec", 0) for a, b in zip(cpu, before))
    user_usec = sum(a.get("user_usec", 0) - b.get("user_usec", 0) for a, b in zip(cpu, before))
    system_usec = sum(a.get("system_usec", 0) - b.get("system_usec", 0) for a, b in zip(cpu, before))
    pmu = {k: sum((x.get("pmu") or {}).get(k, 0) for x in reps) for k in ("cycles", "instructions", "LLC-loads", "LLC-load-misses")}
    ratios = [v for x in reps for v in (x.get("pmu_running_ratio") or {}).values()]
    memory = [x.get("memory_stat", {}) for x in reps]
    current = [x.get("memory_current_bytes") for x in reps if x.get("memory_current_bytes") is not None]
    peak = [x.get("memory_peak_bytes") for x in reps if x.get("memory_peak_bytes") is not None]
    anon = [x.get("anon", 0) for x in memory]
    file_ = [x.get("file", 0) for x in memory]
    loads = pmu["LLC-loads"]
    misses = pmu["LLC-load-misses"]
    ins = pmu["instructions"]
    cycles = pmu["cycles"]
    return {
        "candidate": row.get("candidate"), "band": row.get("R_band"),
        "round": row.get("round"), "attempt": row.get("attempt"),
        "placement": row.get("placement"), "placement_variant": row.get("placement_variant"),
        "status": row.get("status"), "makespan_s": row.get("makespan_s"),
        "worker_duration_median_s": statistics.median(x.get("duration_s", 0) for x in reps) if reps else None,
        "worker_duration_max_s": max((x.get("duration_s", 0) for x in reps), default=None),
        "cpu_time_s": usage_usec / 1e6, "user_cpu_s": user_usec / 1e6, "system_cpu_s": system_usec / 1e6,
        "cycles": cycles, "instructions": ins, "ipc": ins / cycles if cycles else None,
        "llc_loads": loads, "llc_load_misses": misses,
        "llc_miss_rate": misses / loads if loads else None,
        "llc_mpki": misses * 1000 / ins if ins else None,
        "llc_loads_per_cpu_s": loads / (usage_usec / 1e6) if usage_usec else None,
        "llc_misses_per_cpu_s": misses / (usage_usec / 1e6) if usage_usec else None,
        "pmu_running_ratio_min": min(ratios) if ratios else None,
        "memory_current_median_bytes": statistics.median(current) if current else None,
        "memory_current_max_bytes": max(current) if current else None,
        "memory_peak_median_bytes": statistics.median(peak) if peak else None,
        "memory_peak_max_bytes": max(peak) if peak else None,
        "memory_anon_median_bytes": statistics.median(anon) if anon else None,
        "memory_file_median_bytes": statistics.median(file_) if file_ else None,
        "replica_count": len(reps),
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: analyze_placement_trials.py OUT_DIR ROUNDS_JSONL [...]")
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    raw: list[dict] = []
    for name in sys.argv[2:]:
        for line in Path(name).read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict) and value.get("replicas"):
                    raw.append(value)
    trial_rows = [trial_row(x) for x in raw]
    write_csv(out / "trial_metrics.csv", trial_rows)

    grouped: dict[tuple, dict[str, list[float]]] = {}
    for x in trial_rows:
        key = (x["candidate"], x["placement"])
        grouped.setdefault(key, {})
        for field in ("makespan_s", "ipc", "llc_miss_rate", "llc_mpki", "llc_loads_per_cpu_s", "llc_misses_per_cpu_s", "cpu_time_s", "memory_current_median_bytes", "memory_peak_median_bytes", "memory_anon_median_bytes", "memory_file_median_bytes"):
            value = x.get(field)
            if value is not None:
                grouped[key].setdefault(field, []).append(float(value))
    summary = []
    candidates = sorted({x["candidate"] for x in trial_rows})
    for candidate in candidates:
        placements = {p: grouped.get((candidate, p), {}) for p in ("same-slice", "2-slice", "4-slice")}
        row = {"candidate": candidate}
        for p, values in placements.items():
            row[f"{p}_n"] = len(values.get("makespan_s", []))
            for field in ("makespan_s", "ipc", "llc_miss_rate", "llc_mpki", "llc_loads_per_cpu_s", "llc_misses_per_cpu_s", "cpu_time_s", "memory_current_median_bytes", "memory_peak_median_bytes", "memory_anon_median_bytes", "memory_file_median_bytes"):
                row[f"{p}_{field}_median"] = statistics.median(values[field]) if values.get(field) else None
        same = row.get("same-slice_makespan_s_median")
        four = row.get("4-slice_makespan_s_median")
        row["same_over_4_speedup"] = same / four if same and four else None
        summary.append(row)
    write_csv(out / "summary.csv", summary)
    print(json.dumps({"trials": len(trial_rows), "candidates": candidates, "out": str(out)}, indent=2))


if __name__ == "__main__":
    main()
