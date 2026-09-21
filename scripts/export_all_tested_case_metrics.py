"""Export every locally available verified trial and replica metric."""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path


ROOT = Path(r"C:\Users\29068\Desktop\ClawBox")
ART = ROOT / ".artifacts" / "llc-placement-discovery-20260914"
OUT = ART / "all_tested_case_metrics"


def parse_stat(text: str) -> dict[str, float]:
    out = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) == 2:
            try:
                out[p[0]] = float(p[1])
            except ValueError:
                pass
    return out


def numa_value(text: str, key: str) -> float | None:
    for line in text.splitlines():
        if line.startswith(key + " "):
            for token in line.split():
                if token.startswith("N0="):
                    return float(token.split("=", 1)[1])
    return None


def write(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fields = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def json_trial_rows(path: Path, dataset: str, commands: dict[str, str]) -> tuple[list[dict], list[dict]]:
    trials, replicas = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not row.get("replicas"):
            continue
        reps = list(row["replicas"].items())
        durations = [float(x.get("duration_s", 0)) for _, x in reps]
        usage = []
        user = []
        system = []
        memory_current = []
        memory_peak = []
        memory_anon = []
        memory_file = []
        pmu = {k: 0 for k in ("cycles", "instructions", "LLC-loads", "LLC-load-misses")}
        ratios = []
        for worker, x in reps:
            before = parse_stat(x.get("cpu_stat_before", ""))
            after = parse_stat(x.get("cpu_stat_after", ""))
            usage.append(after.get("usage_usec", 0) - before.get("usage_usec", 0))
            user.append(after.get("user_usec", 0) - before.get("user_usec", 0))
            system.append(after.get("system_usec", 0) - before.get("system_usec", 0))
            for k in pmu:
                pmu[k] += int((x.get("pmu") or {}).get(k, 0))
            ratios.extend((x.get("pmu_running_ratio") or {}).values())
            if x.get("memory_current_bytes") is not None:
                memory_current.append(float(x["memory_current_bytes"]))
            if x.get("memory_peak_bytes") is not None:
                memory_peak.append(float(x["memory_peak_bytes"]))
            stat = x.get("memory_stat") or {}
            if "anon" in stat:
                memory_anon.append(float(stat["anon"]))
            if "file" in stat:
                memory_file.append(float(stat["file"]))
            replicas.append({
                "dataset": dataset, "candidate": row.get("candidate"), "round": row.get("round"),
                "attempt": row.get("attempt"), "placement": row.get("placement"),
                "placement_variant": row.get("placement_variant"), "worker": worker,
                "completion_s": x.get("duration_s"), "cpu_usage_s": usage[-1] / 1e6,
                "cpu_user_s": user[-1] / 1e6, "cpu_system_s": system[-1] / 1e6,
                "cycles": (x.get("pmu") or {}).get("cycles"), "instructions": (x.get("pmu") or {}).get("instructions"),
                "ipc": ((x.get("pmu") or {}).get("instructions", 0) / (x.get("pmu") or {}).get("cycles", 1)),
                "llc_loads": (x.get("pmu") or {}).get("LLC-loads"), "llc_load_misses": (x.get("pmu") or {}).get("LLC-load-misses"),
                "llc_miss_rate": ((x.get("pmu") or {}).get("LLC-load-misses", 0) / (x.get("pmu") or {}).get("LLC-loads", 1)),
                "llc_mpki": ((x.get("pmu") or {}).get("LLC-load-misses", 0) * 1000 / (x.get("pmu") or {}).get("instructions", 1)),
                "llc_running_ratio": (x.get("pmu_running_ratio") or {}).get("LLC-loads"),
                "llc_miss_running_ratio": (x.get("pmu_running_ratio") or {}).get("LLC-load-misses"),
                "cycles_running_ratio": (x.get("pmu_running_ratio") or {}).get("cycles"),
                "instructions_running_ratio": (x.get("pmu_running_ratio") or {}).get("instructions"),
                "memory_current_bytes": x.get("memory_current_bytes"), "memory_peak_bytes": x.get("memory_peak_bytes"),
                "memory_stat_anon_bytes": stat.get("anon"), "memory_stat_file_bytes": stat.get("file"),
                "memory_numa_anon_N0_bytes": numa_value(x.get("memory_numa_stat", ""), "anon"),
                "memory_numa_file_N0_bytes": numa_value(x.get("memory_numa_stat", ""), "file"),
                "intended_cpus": json.dumps(x.get("intended_cpus")), "effective_cpus": json.dumps(x.get("effective_cpus")),
                "effective_mems": json.dumps(x.get("effective_mems")), "checkpoint_verified": x.get("checkpoint_verified"),
                "mapping_verified": x.get("mapping_verified"), "output_digest_match": x.get("output_digest_match"),
            })
        cycles = pmu["cycles"]
        instructions = pmu["instructions"]
        loads = pmu["LLC-loads"]
        misses = pmu["LLC-load-misses"]
        trials.append({
            "dataset": dataset, "candidate": row.get("candidate"), "round": row.get("round"), "attempt": row.get("attempt"),
            "placement": row.get("placement"), "placement_variant": row.get("placement_variant"), "status": row.get("status"),
            "replica_count": len(reps), "makespan_s": row.get("makespan_s"), "completion_min_s": min(durations),
            "completion_median_s": statistics.median(durations), "completion_max_s": max(durations),
            "throughput_tools_per_s": 4 / float(row.get("makespan_s")) if row.get("makespan_s") else None,
            "cpu_time_s_sum": sum(usage) / 1e6, "cpu_user_s_sum": sum(user) / 1e6, "cpu_system_s_sum": sum(system) / 1e6,
            "cycles_sum": cycles, "instructions_sum": instructions, "ipc": instructions / cycles if cycles else None,
            "llc_loads_sum": loads, "llc_load_misses_sum": misses, "llc_miss_rate": misses / loads if loads else None,
            "llc_mpki": misses * 1000 / instructions if instructions else None,
            "llc_loads_per_cpu_s_M": loads / (sum(usage) / 1e6) / 1e6 if sum(usage) else None,
            "llc_misses_per_cpu_s_M": misses / (sum(usage) / 1e6) / 1e6 if sum(usage) else None,
            "pmu_running_ratio_min": min(ratios) if ratios else None, "pmu_running_ratio_mean": statistics.mean(ratios) if ratios else None,
            "memory_current_median_bytes": statistics.median(memory_current) if memory_current else None,
            "memory_current_max_bytes": max(memory_current) if memory_current else None,
            "memory_peak_median_bytes": statistics.median(memory_peak) if memory_peak else None,
            "memory_peak_max_bytes": max(memory_peak) if memory_peak else None,
            "memory_stat_anon_median_bytes": statistics.median(memory_anon) if memory_anon else None,
            "memory_stat_file_median_bytes": statistics.median(memory_file) if memory_file else None,
            "command": commands.get(row.get("candidate"), ""),
        })
    return trials, replicas


def main() -> None:
    OUT.mkdir(exist_ok=True)
    trial_rows, replica_rows = [], []

    workload_csv = {r["candidate"]: r for r in csv.DictReader((ART / "audit" / "audit_workloads.csv").open(encoding="utf-8"))}
    old_trials = list(csv.DictReader((ART / "audit" / "audit_trial_metrics.csv").open(encoding="utf-8")))
    old_reps = list(csv.DictReader((ART / "audit" / "audit_replica_metrics.csv").open(encoding="utf-8")))
    for r in old_trials:
        x = dict(r); x["dataset"] = "real-agent-verified-old-run"; x["command"] = workload_csv[r["candidate"]]["command"]
        x["memory_current_median_bytes"] = ""; x["memory_peak_median_bytes"] = ""; x["memory_stat_anon_median_bytes"] = ""; x["memory_stat_file_median_bytes"] = ""
        x["llc_loads_per_cpu_s_M"] = float(r["llc_read"]) / float(r["cpu_s_sum"]) / 1e6
        x["llc_misses_per_cpu_s_M"] = float(r["llc_miss"]) / float(r["cpu_s_sum"]) / 1e6
        x["replica_count"] = 4
        trial_rows.append(x)
    for r in old_reps:
        x = dict(r); x["dataset"] = "real-agent-verified-old-run"
        x["memory_current_bytes"] = ""; x["memory_peak_bytes"] = ""; x["memory_stat_anon_bytes"] = ""; x["memory_stat_file_bytes"] = ""
        x["memory_numa_anon_N0_bytes"] = numa_value("\n".join(f"{k} " + " ".join(f"N{i}={v}" for i,v in enumerate([])) for k,v in []), "anon")
        s = json.loads(r["memory_numa_stat_json"])
        x["memory_numa_anon_N0_bytes"] = s.get("anon", {}).get("N0")
        x["memory_numa_file_N0_bytes"] = s.get("file", {}).get("N0")
        x["memory_stat_anon_bytes"] = ""; x["memory_stat_file_bytes"] = ""
        replica_rows.append(x)

    commands = {name: target["command"] for name, target in json.loads((ART / "working_set_plan.json").read_text(encoding="utf-8"))["targets"].items()}
    for path, dataset in [
        (ART / "working_set_sweep_partial_rounds.jsonl", "synthetic-pointer-partial"),
        (ART / "working_set_sanity" / "rounds.jsonl", "synthetic-pointer-sanity-old"),
        (ART / "working_set_sanity" / "rounds-v2.jsonl", "synthetic-pointer-sanity-v2"),
        (ART / "working_set_sanity" / "stream-v2-rounds.jsonl", "synthetic-stream-sanity-v2"),
        (ART / "working_set_sanity" / "stream-v3-rounds.jsonl", "synthetic-stream-sanity-v3"),
    ]:
        t, r = json_trial_rows(path, dataset, commands)
        trial_rows.extend(t); replica_rows.extend(r)
    write(OUT / "all_tested_case_repetitions.csv", trial_rows)
    write(OUT / "all_tested_case_replicas.csv", replica_rows)

    status = [
        {"case": "3d-model-format-legacy", "status": "verified", "detail": "3 placements x 3 repetitions; weak positive"},
        {"case": "accelerate-maximal-square", "status": "verified", "detail": "3 placements x 3 repetitions; weak positive"},
        {"case": "blind-maze-explorer-algorithm", "status": "verified", "detail": "3 placements x 3 repetitions; weak positive; full check.sh, not 120s timeout"},
        {"case": "scim2-flake8", "status": "no-valid-PMU-trial", "detail": "checkpoint stdout digest mismatch involving exit=0 vs exit=0 newline"},
        {"case": "scim2-flake8-screening-timing-note", "status": "conversation-only-not-audit-row", "detail": "Earlier conversation recorded same-slice 8.860356/8.867197/8.872501 s and 4-slice 8.594740/8.643682/8.605950 s; no local PMU/memory/trial.json was retained, so it is excluded from verified metrics."},
        {"case": "spectree-parse-params", "status": "prepared-not-measured", "detail": "timing/PMU launch paused"},
        {"case": "pointer-chase-1MiB", "status": "partial-synthetic", "detail": "old sanity had same/2 verified and 4-slice failed; v2 had all 3 verified; final sweep stopped after 64 verified trials"},
        {"case": "sequential-stream-512MiB", "status": "sanity-only", "detail": "512MiB sequential control; v2 was <10s in fastest placement, v3 reached about 10.2s"},
        {"case": "django-read-migrations", "status": "not-run", "detail": "fresh real-tool screening plan prepared; Kunpeng unreachable before launch"},
        {"case": "hyp3-pytest-suite", "status": "not-run", "detail": "fresh real-tool screening plan prepared; Kunpeng unreachable before launch"},
    ]
    write(OUT / "all_tested_case_status.csv", status)
    print(json.dumps({"trial_rows": len(trial_rows), "replica_rows": len(replica_rows), "out": str(OUT)}, indent=2))


if __name__ == "__main__":
    main()
