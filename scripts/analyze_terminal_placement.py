"""Flatten placement-replica trial JSON and calculate timing/PMU summaries."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


WORKERS = ("w0", "w1", "w2", "w3")
PLACEMENTS = ("same-slice", "2-slice", "4-slice")


def stat_value(text: str, key: str) -> float:
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name == key:
            return float(value)
    return 0.0


def pct_change(new: float, old: float) -> float | None:
    return (new / old - 1.0) * 100.0 if old else None


def trial_row(path: Path) -> dict:
    trial = json.loads(path.read_text(encoding="utf-8"))
    replicas = trial["replicas"]
    pmu_sum = {name: 0 for name in ("LLC-loads", "LLC-load-misses", "cycles", "instructions")}
    cpu_s = 0.0
    ratios = []
    replica_rows = []
    for worker in WORKERS:
        replica = replicas[worker]
        before = replica.get("cpu_stat_before", "")
        after = replica.get("cpu_stat_after", "")
        usage_usec = max(0.0, stat_value(after, "usage_usec") - stat_value(before, "usage_usec"))
        cpu_s += usage_usec / 1_000_000.0
        for event, value in replica["pmu"].items():
            pmu_sum[event] += int(value)
        ratios.extend(replica.get("pmu_running_ratio", {}).values())
        cycles = replica["pmu"]["cycles"]
        instructions = replica["pmu"]["instructions"]
        replica_rows.append(
            {
                "candidate": trial["candidate"],
                "round": trial["round"],
                "placement": trial["placement"],
                "attempt": trial["attempt"],
                "worker": worker,
                "completion_s": replica["duration_s"],
                "cpu_s": usage_usec / 1_000_000.0,
                "cycles": cycles,
                "instructions": instructions,
                "ipc": instructions / cycles if cycles else None,
                "llc_read": replica["pmu"]["LLC-loads"],
                "llc_miss": replica["pmu"]["LLC-load-misses"],
                "llc_miss_rate": replica["pmu"]["LLC-load-misses"] / replica["pmu"]["LLC-loads"] if replica["pmu"]["LLC-loads"] else None,
                "llc_mpki": replica["pmu"]["LLC-load-misses"] * 1000.0 / instructions if instructions else None,
                "pmu_running_ratio_min": min(replica.get("pmu_running_ratio", {}).values()),
                "output_digest_match": replica["output_digest_match"],
                "effective_cpus": ";".join(map(str, replica["effective_cpus"])),
                "effective_mems": ";".join(map(str, replica["effective_mems"])),
            }
        )
    cycles = pmu_sum["cycles"]
    instructions = pmu_sum["instructions"]
    reads = pmu_sum["LLC-loads"]
    misses = pmu_sum["LLC-load-misses"]
    row = {
        "candidate": trial["candidate"],
        "round": trial["round"],
        "placement": trial["placement"],
        "attempt": trial["attempt"],
        "status": trial["status"],
        "makespan_s": trial["makespan_s"],
        "throughput_tools_per_s": 4.0 / trial["makespan_s"],
        "w0_completion_s": replicas["w0"]["duration_s"],
        "w1_completion_s": replicas["w1"]["duration_s"],
        "w2_completion_s": replicas["w2"]["duration_s"],
        "w3_completion_s": replicas["w3"]["duration_s"],
        "cpu_s_sum": cpu_s,
        "cycles_sum": cycles,
        "instructions_sum": instructions,
        "ipc": instructions / cycles if cycles else None,
        "llc_read": reads,
        "llc_miss": misses,
        "llc_miss_rate": misses / reads if reads else None,
        "llc_mpki": misses * 1000.0 / instructions if instructions else None,
        "pmu_running_ratio_min": min(ratios),
        "pmu_running_ratio_mean": statistics.mean(ratios),
        "all_output_digest_match": all(replica["output_digest_match"] for replica in replicas.values()),
        "plan_sha256": trial["plan_sha256"],
        "run_label": trial["run_label"],
        "numa_node": trial["numa_node"],
        "cpu_offset": trial["cpu_offset"],
    }
    return {"trial": row, "replicas": replica_rows}


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def median(rows: list[dict], key: str) -> float:
    return statistics.median(float(row[key]) for row in rows)


def make_summary(rows: list[dict]) -> list[dict]:
    out = []
    candidates = sorted({row["candidate"] for row in rows})
    for candidate in candidates:
        by_place = {
            placement: [row for row in rows if row["candidate"] == candidate and row["placement"] == placement]
            for placement in PLACEMENTS
        }
        same_by_round = {row["round"]: row for row in by_place["same-slice"]}
        four_by_round = {row["round"]: row for row in by_place["4-slice"]}
        n_better = sum(four_by_round[r]["makespan_s"] < same_by_round[r]["makespan_s"] for r in same_by_round.keys() & four_by_round.keys())
        row = {"candidate": candidate, "n_rounds": len(by_place["same-slice"]), "n_4slice_better": n_better}
        for placement in PLACEMENTS:
            subset = by_place[placement]
            for key in ("makespan_s", "throughput_tools_per_s", "cpu_s_sum", "ipc", "llc_miss_rate", "llc_mpki", "pmu_running_ratio_mean"):
                row[f"{placement}_{key}_median"] = median(subset, key) if subset else None
        if by_place["same-slice"] and by_place["4-slice"]:
            same_time = row["same-slice_makespan_s_median"]
            four_time = row["4-slice_makespan_s_median"]
            row["same_over_4_speedup"] = same_time / four_time
            row["4slice_minus_same_ipc_pct"] = pct_change(row["4-slice_ipc_median"], row["same-slice_ipc_median"])
            row["4slice_minus_same_mpki_pct"] = pct_change(row["4-slice_llc_mpki_median"], row["same-slice_llc_mpki_median"])
        else:
            row["same_over_4_speedup"] = None
            row["4slice_minus_same_ipc_pct"] = None
            row["4slice_minus_same_mpki_pct"] = None
        row["screening_10pct_pass"] = bool(row.get("same_over_4_speedup") and row["same_over_4_speedup"] >= 1.10 and n_better >= 2)
        out.append(row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    trial_payloads = []
    for path in sorted(args.root.glob("*/trial.json")):
        try:
            trial_payloads.append(trial_row(path))
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            print(f"skip {path}: {exc}")
    trial_rows = [item["trial"] for item in trial_payloads]
    replica_rows = [row for item in trial_payloads for row in item["replicas"]]
    write_csv(args.out / "raw_repetitions.csv", trial_rows)
    write_csv(args.out / "raw_replicas.csv", replica_rows)
    write_csv(args.out / "summary.csv", make_summary(trial_rows))
    print(json.dumps(make_summary(trial_rows), indent=2))


if __name__ == "__main__":
    main()
