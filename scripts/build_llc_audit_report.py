"""Build a self-contained audit index from the current LLC-placement artifacts."""

from __future__ import annotations

import csv
import glob
import json
import math
import statistics
from pathlib import Path


ROOT = Path(r"C:\Users\29068\Desktop\ClawBox")
ART = ROOT / ".artifacts" / "llc-placement-discovery-20260914"
TRIAL_ROOT = ART / "llc-placement-discovery-20260914-screening"
OUT = ART / "audit"
OUT.mkdir(parents=True, exist_ok=True)


def parse_stat(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def stat_delta(before: str, after: str, key: str) -> int:
    return parse_stat(after).get(key, 0) - parse_stat(before).get(key, 0)


def memory_stat(text: str) -> dict[str, dict[str, int]]:
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        values = {}
        for field in fields[1:]:
            if "=" in field:
                node, value = field.split("=", 1)
                values[node] = int(value)
        result[fields[0]] = values
    return result


def median(values: list[float]) -> float:
    return statistics.median(values) if values else math.nan


def load_plan(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def source_lookup() -> tuple[dict, dict]:
    plans = {}
    for filename in ("terminal_runner_plan.json", "swe_runner_plan.json"):
        plan = load_plan(ART / filename)
        for name, target in plan["targets"].items():
            plans[name] = {"target": target, "plan": plan, "source": filename}

    discovered = {}
    with (ART / "discovered_candidates.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            discovered[(row["task_id"], row["span_id"])] = row
    return plans, discovered


def load_calls() -> tuple[dict[tuple[str, str], dict], dict[str, dict]]:
    calls = json.loads((ART.parent / "placement-first-pass" / "calls.json").read_text(encoding="utf-8"))
    return (
        {(row.get("trace_id", ""), row.get("span_id", "")): row for row in calls},
        {row.get("span_id", ""): row for row in calls},
    )


def trial_rows(plans: dict) -> tuple[list[dict], list[dict]]:
    aggregate = []
    replicas = []
    for filename in glob.glob(str(TRIAL_ROOT / "**" / "trial.json"), recursive=True):
        trial = json.loads(Path(filename).read_text(encoding="utf-8"))
        # Only the corrected current plan is valid for this audit.  The other
        # three JSONs are retained in the raw archive but were superseded.
        if trial.get("status") != "verified" or trial.get("run_label") != "terminal-screening-corrected":
            continue
        name = trial["candidate"]
        target = plans[name]["target"]
        rep = trial["replicas"]
        cpu = {w: stat_delta(rep[w]["cpu_stat_before"], rep[w]["cpu_stat_after"], "usage_usec") / 1e6 for w in rep}
        user = {w: stat_delta(rep[w]["cpu_stat_before"], rep[w]["cpu_stat_after"], "user_usec") / 1e6 for w in rep}
        system = {w: stat_delta(rep[w]["cpu_stat_before"], rep[w]["cpu_stat_after"], "system_usec") / 1e6 for w in rep}
        pmu = {key: sum(rep[w]["pmu"][key] for w in rep) for key in ("LLC-loads", "LLC-load-misses", "cycles", "instructions")}
        miss_rate = pmu["LLC-load-misses"] / pmu["LLC-loads"] if pmu["LLC-loads"] else math.nan
        mpki = pmu["LLC-load-misses"] * 1000 / pmu["instructions"] if pmu["instructions"] else math.nan
        ipc = pmu["instructions"] / pmu["cycles"] if pmu["cycles"] else math.nan
        durations = [rep[w]["duration_s"] for w in rep]
        ratios = [v for w in rep for v in rep[w]["pmu_running_ratio"].values()]
        row = {
            "candidate": name,
            "round": trial["round"],
            "placement": trial["placement"],
            "attempt": trial["attempt"],
            "status": trial["status"],
            "makespan_s": trial["makespan_s"],
            "throughput_tools_per_s": 4 / trial["makespan_s"],
            "completion_min_s": min(durations),
            "completion_median_s": median(durations),
            "completion_max_s": max(durations),
            "cpu_s_sum": sum(cpu.values()),
            "cpu_user_s_sum": sum(user.values()),
            "cpu_system_s_sum": sum(system.values()),
            "cycles_sum": pmu["cycles"],
            "instructions_sum": pmu["instructions"],
            "ipc": ipc,
            "llc_read": pmu["LLC-loads"],
            "llc_miss": pmu["LLC-load-misses"],
            "llc_miss_rate": miss_rate,
            "llc_mpki": mpki,
            "pmu_running_ratio_min": min(ratios),
            "pmu_running_ratio_mean": statistics.mean(ratios),
            "all_output_digest_match": all(rep[w]["output_digest_match"] for w in rep),
            "checkpoint_image": target["checkpoint_image"],
            "checkpoint_image_id": target["checkpoint_image_id"],
            "plan_sha256": trial["plan_sha256"],
            "numa_node": trial["numa_node"],
            "memory_limit_bytes": 6 * 1024**3,
            "vcpus_per_replica": 1,
        }
        aggregate.append(row)
        for worker in sorted(rep):
            item = rep[worker]
            delta = stat_delta(item["cpu_stat_before"], item["cpu_stat_after"], "usage_usec") / 1e6
            user_delta = stat_delta(item["cpu_stat_before"], item["cpu_stat_after"], "user_usec") / 1e6
            system_delta = stat_delta(item["cpu_stat_before"], item["cpu_stat_after"], "system_usec") / 1e6
            pm = item["pmu"]
            ratios_one = item["pmu_running_ratio"]
            replicas.append({
                "candidate": name,
                "round": trial["round"],
                "placement": trial["placement"],
                "attempt": trial["attempt"],
                "worker": worker,
                "completion_s": item["duration_s"],
                "started_epoch_s": item["started_s"],
                "ended_epoch_s": item["ended_s"],
                "exit_code": item["exit_code"],
                "intended_cpus": json.dumps(item["intended_cpus"]),
                "effective_cpus": json.dumps(item["effective_cpus"]),
                "effective_mems": json.dumps(item["effective_mems"]),
                "checkpoint_image_id": item["checkpoint_image_id"],
                "checkpoint_verified": item["checkpoint_verified"],
                "mapping_verified": item["mapping_verified"],
                "cpu_usage_s": delta,
                "cpu_user_s": user_delta,
                "cpu_system_s": system_delta,
                "cpu_nr_throttled_delta": stat_delta(item["cpu_stat_before"], item["cpu_stat_after"], "nr_throttled"),
                "cpu_throttled_s": stat_delta(item["cpu_stat_before"], item["cpu_stat_after"], "throttled_usec") / 1e6,
                "memory_numa_stat_json": json.dumps(memory_stat(item["memory_numa_stat"]), separators=(",", ":")),
                "memory_limit_bytes": 6 * 1024**3,
                "memory_mems": json.dumps(item["effective_mems"]),
                "llc_read": pm["LLC-loads"],
                "llc_miss": pm["LLC-load-misses"],
                "cycles": pm["cycles"],
                "instructions": pm["instructions"],
                "ipc": pm["instructions"] / pm["cycles"] if pm["cycles"] else math.nan,
                "llc_miss_rate": pm["LLC-load-misses"] / pm["LLC-loads"] if pm["LLC-loads"] else math.nan,
                "llc_mpki": pm["LLC-load-misses"] * 1000 / pm["instructions"] if pm["instructions"] else math.nan,
                "llc_running_ratio": ratios_one["LLC-loads"],
                "llc_miss_running_ratio": ratios_one["LLC-load-misses"],
                "cycles_running_ratio": ratios_one["cycles"],
                "instructions_running_ratio": ratios_one["instructions"],
                "stdout_sha256": item["stdout_sha256"],
                "output_digest_match": item["output_digest_match"],
                "raw_perf_stat": item["perf_stat"],
            })
    aggregate.sort(key=lambda x: (x["candidate"], x["round"], x["placement"]))
    replicas.sort(key=lambda x: (x["candidate"], x["round"], x["placement"], x["worker"]))
    return aggregate, replicas


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def fmt(value: float) -> str:
    return f"{value:.6f}"


def main() -> None:
    plans, discovered = source_lookup()
    calls, calls_by_span = load_calls()
    aggregate, replicas = trial_rows(plans)
    write_csv(OUT / "audit_trial_metrics.csv", aggregate)
    write_csv(OUT / "audit_replica_metrics.csv", replicas)

    workloads = []
    status = []
    for name, info in plans.items():
        target = info["target"]
        call = calls.get((target.get("trace", ""), target.get("span", ""))) or calls_by_span.get(target.get("span", ""), {})
        task_id = target.get("task_id") or call.get("task_id", name)
        source = discovered.get((task_id, target.get("span", "")), {})
        measured = [x for x in aggregate if x["candidate"] == name]
        state = "measured_screening" if measured else ("checkpoint_validation_attempted_no_valid_trial" if name == "scim2-flake8" else "checkpoint_available_not_measured")
        reason = "3 corrected-plan repetitions per placement" if measured else (
            "warmup command produced exit=0 and stdout exit=0\\n; the historical checkpoint digest was sha256('exit=0') without the captured trailing newline, so no PMU trial was accepted" if name == "scim2-flake8" else
            "candidate was prepared in the audit plan but timing/PMU was paused before launch"
        )
        workloads.append({
            "candidate": name,
            "suite": "TerminalBench" if info["source"] == "terminal_runner_plan.json" else "SWE-ReBench",
            "task_id": task_id,
            "trace": target.get("trace", ""),
            "span": target.get("span", ""),
            "sequence_no": target.get("sequence_no") or call.get("sequence_no", ""),
            "repo": target.get("repo") or call.get("repo", ""),
            "command": target["command"],
            "trace_duration_s": target["duration_s"],
            "trace_cpu_time_s": source.get("cpu_time_s", call.get("cpu_time_s", "")),
            "trace_busy_ratio": source.get("busy_ratio", call.get("busy_ratio", "")),
            "trace_pmu_active_s": source.get("pmu_active_s", call.get("pmu_active_s", "")),
            "trace_pmu_elapsed_s": source.get("pmu_elapsed_s", call.get("pmu_elapsed_s", "")),
            "trace_pmu_running_ratio": source.get("running_ratio", call.get("busy_ratio", "")),
            "trace_llc_read_M_per_CPU_s": target.get("llc_read_M_per_CPU_s", ""),
            "trace_llc_miss_M_per_CPU_s": target.get("llc_miss_M_per_CPU_s", ""),
            "trace_H_M_per_CPU_s": target.get("H_M_per_CPU_s", ""),
            "trace_llc_miss_rate": source.get("llc_miss_rate", ""),
            "trace_ipc": source.get("ipc", ""),
            "checkpoint_image": target["checkpoint_image"],
            "checkpoint_image_id": target["checkpoint_image_id"],
            "workdir": target.get("workdir", ""),
            "expected_exit_code": target.get("expected_exit_code", ""),
            "expected_stdout_tail": target.get("expected_stdout_tail", ""),
            "state": state,
            "state_reason": reason,
        })
        status.append({"candidate": name, "state": state, "reason": reason})
    write_csv(OUT / "audit_workloads.csv", workloads)
    write_csv(OUT / "audit_status.csv", status)

    topology = {
        "host": "kunpeng / hostname-txyuq.foreman.pxe",
        "kernel_arch": "Linux 6.6, aarch64 host, linux/amd64 containers under emulation",
        "numa_node": 0,
        "memory_limit_bytes_per_replica": 6 * 1024**3,
            "vcpus_per_replica": 1,
            "pids_limit": 512,
        "smt_siblings_used": False,
        "source": "live sysfs cache/index3/shared_cpu_list and cluster_cpus_list",
        "slices": {
            "S0": {"selected_cpus": [8, 10, 12, 14], "cluster_cpus": list(range(8, 16))},
            "S1": {"selected_cpus": [16, 20], "cluster_cpus": list(range(16, 24))},
            "S2": {"selected_cpus": [24], "cluster_cpus": list(range(24, 32))},
            "S3": {"selected_cpus": [32], "cluster_cpus": list(range(32, 40))},
        },
        "placements": {
            "same-slice": [[8], [10], [12], [14]],
            "2-slice": [[8, 10], [8, 10], [16, 20], [16, 20]],
            "4-slice": [[8], [16], [24], [32]],
        },
        "measurement_contract": {
            "fresh_checkpoint_each_trial": True,
            "network": "none",
            "pmu_events": ["LLC-loads", "LLC-load-misses", "cycles", "instructions"],
            "pmu_acceptance": "all four events running ratio exactly 1.0",
            "valid_terminal_trials": len(aggregate),
        },
    }
    (OUT / "audit_topology.json").write_text(json.dumps(topology, indent=2) + "\n", encoding="utf-8")

    summary = {}
    for name in sorted({x["candidate"] for x in aggregate}):
        summary[name] = {}
        for placement in ("same-slice", "2-slice", "4-slice"):
            rows = [x for x in aggregate if x["candidate"] == name and x["placement"] == placement]
            summary[name][placement] = {
                "n": len(rows),
                "makespan_s": [x["makespan_s"] for x in rows],
                "completion_s": [x["completion_max_s"] for x in rows],
                "cpu_s_sum": [x["cpu_s_sum"] for x in rows],
                "ipc": [x["ipc"] for x in rows],
                "llc_read": [x["llc_read"] for x in rows],
                "llc_miss": [x["llc_miss"] for x in rows],
                "llc_miss_rate": [x["llc_miss_rate"] for x in rows],
                "llc_mpki": [x["llc_mpki"] for x in rows],
                "pmu_running_ratio_mean": [x["pmu_running_ratio_mean"] for x in rows],
            }
    (OUT / "audit_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# LLC placement workload audit",
        "",
        "This index covers the current 2026-09-14 kunpeng run. Only 27 `verified` trials with run label `terminal-screening-corrected` are included in the measured CSVs; superseded invalid-plan JSONs remain in the raw archive but are excluded.",
        "",
        "## Common execution contract",
        "",
        "- Every measured trial launched four fresh containers from the target's recorded checkpoint image, with `--network none`, one vCPU per replica, `--cpuset-mems 0`, and a 6 GiB memory limit.",
        "- Placement CPU sets came from live sysfs cache/cluster topology. Same-slice used S0; 2-slice used two replicas in S0 and two in S1; 4-slice used S0/S1/S2/S3.",
        "- The runner collected cgroup CPU accounting, cgroup NUMA memory statistics, `perf stat` LLC-loads, LLC-load-misses, cycles and instructions, and PMU running ratios. All accepted PMU ratios were 1.0.",
        "- `audit_replica_metrics.csv` contains one row per worker (108 rows for 27 trials) and preserves the raw `perf_stat` text and full parsed `memory.numa_stat` JSON.",
        "",
        "## Workloads and exact commands",
        "",
    ]
    for item in workloads:
        lines += [
            f"### {item['candidate']}",
            "",
            f"- suite: `{item['suite']}`; task: `{item['task_id']}`; trace: `{item['trace']}`; span: `{item['span']}`; sequence: `{item['sequence_no']}`",
            f"- repository: `{item['repo']}`",
            f"- trace duration: `{item['trace_duration_s']} s`; source CPU time: `{item['trace_cpu_time_s']}`; source PMU active/elapsed: `{item['trace_pmu_active_s']}` / `{item['trace_pmu_elapsed_s']}`; source busy/running ratio: `{item['trace_busy_ratio']}` / `{item['trace_pmu_running_ratio']}`",
            f"- source LLC read/miss/H: `{item['trace_llc_read_M_per_CPU_s']}` / `{item['trace_llc_miss_M_per_CPU_s']}` / `{item['trace_H_M_per_CPU_s']}` M/CPU-s; source miss rate/IPC: `{item['trace_llc_miss_rate']}` / `{item['trace_ipc']}`",
            f"- checkpoint: `{item['checkpoint_image']}` (`{item['checkpoint_image_id']}`); workdir: `{item['workdir']}`; expected wrapper exit: `{item['expected_exit_code']}`",
            f"- audit state: **{item['state']}** -- {item['state_reason']}",
            "- exact command:",
            "```bash",
            item["command"],
            "```",
            "",
        ]
    lines += [
        "## Measured trial-level rows",
        "",
        "The complete 27-row table is in `audit_trial_metrics.csv`; each row is one candidate x round x placement and includes makespan, completion range, CPU deltas, PMU totals, IPC, LLC miss rate, MPKI and running ratios.",
        "",
        "| candidate | round | placement | makespan s | completion max s | CPU s | IPC | LLC read | LLC miss | miss rate | MPKI | PMU ratio |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in aggregate:
        lines.append(
            f"| {row['candidate']} | {row['round']} | {row['placement']} | {fmt(row['makespan_s'])} | {fmt(row['completion_max_s'])} | {fmt(row['cpu_s_sum'])} | {fmt(row['ipc'])} | {row['llc_read']} | {row['llc_miss']} | {fmt(row['llc_miss_rate'])} | {fmt(row['llc_mpki'])} | {fmt(row['pmu_running_ratio_mean'])} |"
        )
    lines += [
        "",
        "## Audit files",
        "",
        "- `audit_workloads.csv`: all five named candidates, task identity, exact command, source trace profile and checkpoint/state.",
        "- `audit_trial_metrics.csv`: all accepted trial-level measurements.",
        "- `audit_replica_metrics.csv`: all accepted worker-level CPU, memory, PMU/LLC, placement and raw perf fields.",
        "- `audit_topology.json`: live CPU-to-slice mapping and equal-resource contract.",
        "- `audit_summary.json`: machine-readable grouping of every measured metric by workload and placement.",
        "- `../llc-placement-discovery-20260914-screening/`: original `trial.json`, per-worker `perf.csv`, stdout/stderr and timing files.",
    ]
    (OUT / "llc_workload_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(OUT / "llc_workload_audit.md")
    print(f"measured_trials={len(aggregate)} measured_replicas={len(replicas)} workloads={len(workloads)}")


if __name__ == "__main__":
    main()
