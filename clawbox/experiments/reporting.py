"""Human-readable, auditable summary of a completed standalone run."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import statistics
from typing import Any

from .spec import load_experiment, load_workload_cases, resolve_case_sandbox


def _offered_capacity_text(spec: Any, concurrency: int) -> str:
    """Describe configured VM capacity for the actual session assignment."""
    cases = load_workload_cases(spec.workload)
    runtime_mib = spec.runtime.memory_mib
    if spec.workload.session_assignment.value == "round_robin":
        offered_mib = sum(
            runtime_mib + resolve_case_sandbox(
                cases[index % len(cases)], spec.sandbox,
            ).memory_mib
            for index in range(concurrency)
        )
        return f"{offered_mib / 1024:g} GiB"
    offered = [
        concurrency * (
            runtime_mib + resolve_case_sandbox(case, spec.sandbox).memory_mib
        )
        for case in cases
    ]
    low, high = min(offered) / 1024, max(offered) / 1024
    return f"{low:g} GiB" if low == high else f"{low:g}-{high:g} GiB"


def priority_summary(results, priority):
    """Keep unsuccessful trials in denominators and resample trial clusters."""
    import random
    clusters = []
    offered = completed = 0
    for result in results:
        row = (result.get('performance', {}).get('priority_sessions') or {}).get(priority, {})
        # A killed worker may not emit per-session data. Offered demand is
        # still known from the frozen arm, and must not disappear.
        concurrency = result.get('arm', {}).get('concurrency', 0)
        offered += row.get('offered', min(1, concurrency) if priority == 'high' else max(0, concurrency-1))
        samples = list(row.get('durations_seconds', []))
        completed += row.get('completed', len(samples))
        clusters.append(samples)
    samples = [value for cluster in clusters for value in cluster]

    def p95(values):
        ordered = sorted(values)
        at = (len(ordered)-1)*.95
        lower = int(at)
        return ordered[lower] + (ordered[min(lower+1, len(ordered)-1)]-ordered[lower])*(at-lower)

    point = p95(samples) if len(samples) >= 20 else None
    interval = (None, None)
    if point is not None and len(clusters) >= 5:
        rng = random.Random(0)
        estimates = []
        for _ in range(1000):
            draw = [v for cluster in rng.choices(clusters, k=len(clusters)) for v in cluster]
            if draw:
                estimates.append(p95(draw))
        estimates.sort()
        if estimates:
            interval = (estimates[int(.025*(len(estimates)-1))], estimates[int(.975*(len(estimates)-1))])
    return {'offered': offered, 'completed': completed, 'latency_samples': len(samples),
            'p95': point, 'p95_interval': interval}


def _number(value: Any, digits: int = 2) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def _gib(value: Any) -> str:
    return "n/a" if value is None else _number(float(value) / 1024 ** 3)


def _distribution(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "n/a"
    return ", ".join(f"{key}:{value[key]}" for key in sorted(value))


def _warm_metrics(events_path: str | None) -> dict[str, int]:
    metrics = {
        "transferred_bytes": 0,
        "private_delta_bytes": 0,
        "unique_base_backing_bytes": 0,
        "base_reference_count": 0,
        "peak_allocated_bytes": 0,
        "peak_committed_bytes": 0,
    }
    bases: dict[str, int] = {}
    if not events_path:
        return metrics
    try:
        stream = Path(events_path).open(encoding="utf-8")
    except OSError:
        return metrics
    with stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") == "memory_sample":
                metrics["peak_allocated_bytes"] = max(
                    metrics["peak_allocated_bytes"],
                    int(row.get("warm_allocated_bytes") or 0),
                )
                metrics["peak_committed_bytes"] = max(
                    metrics["peak_committed_bytes"],
                    int(row.get("warm_committed_bytes") or 0),
                )
            timing = row.get("lifecycle_timing") or {}
            snapshot = timing.get("snapshot_metrics") or {}
            metrics["transferred_bytes"] += int(snapshot.get("transferred_bytes") or 0)
            metrics["private_delta_bytes"] += int(snapshot.get("private_delta_bytes") or 0)
            identity = snapshot.get("external_base_identity")
            if identity:
                metrics["base_reference_count"] += 1
                bases[str(identity)] = max(
                    bases.get(str(identity), 0),
                    int(snapshot.get("external_base_allocated_bytes") or 0),
                )
    metrics["unique_base_backing_bytes"] = sum(bases.values())
    return metrics


def _mean_sd(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return statistics.fmean(values), statistics.stdev(values) if len(values) > 1 else None


def _mean_sd_text(values: list[float], digits: int = 2) -> str:
    mean, sd = _mean_sd(values)
    if mean is None:
        return "n/a"
    if sd is None:
        return f"{mean:.{digits}f} (n=1)"
    return f"{mean:.{digits}f} +/- {sd:.{digits}f}"


def _event_path(run_root: Path, result: dict[str, Any]) -> Path | None:
    raw = (result.get("artifacts") or {}).get("events")
    if raw:
        path = Path(raw)
        if path.is_file():
            return path
    arm_id = str((result.get("arm") or {}).get("arm_id") or "")
    if not arm_id:
        return None
    matches = list((run_root / "attempts").glob(f"*/{arm_id}/events/{arm_id}.jsonl"))
    return matches[0] if len(matches) == 1 else None


def _completed_results(run_root: Path) -> list[dict[str, Any]]:
    results = []
    for path in sorted((run_root / "arms").glob("*.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(result, dict):
            results.append(result)
    return results


def write_memory_timeseries(run_root: Path) -> Path:
    """Export plot-ready memory samples without changing their measurement scope."""
    output = run_root / "memory-timeseries.csv"
    temporary = output.with_name(output.name + ".tmp")
    results = _completed_results(run_root)
    numa_nodes = sorted({node for result in results for node in (
        *[n["numa_node"] for n in result.get("arm", {}).get("resources", {}).get("compute_nodes", [])],
        result.get("arm", {}).get("resources", {}).get("warm_numa_node"),
    ) if node is not None})
    fields = [
        "arm_id", "policy", "repetition", "sample", "elapsed_seconds",
        "local_used_gib", "shared_live_used_gib", "combined_live_used_gib",
        "warm_allocated_gib", "warm_committed_gib", "shared_total_gib",
        "host_used_delta_gib", "host_used_gib", "host_available_gib",
        "cgroup_current_bytes", "anon_bytes", "file_bytes", "shmem_bytes",
        "swap_current_bytes", "pgscan", "pgsteal", "oom", "oom_kill",
        "psi_some_total_us", "psi_full_total_us",
        "cpu_average_cores", "cpu_utilization_fraction", "cpu_nr_throttled",
        "cpu_throttled_usec",
    ]
    fields += [f"numa_{node}_resident_gib" for node in numa_nodes] + ["unattributed_gib"]
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in results:
            arm = result.get("arm") or {}
            path = _event_path(run_root, result)
            if path is None:
                continue
            first_time: int | None = None
            sample = 0
            with path.open(encoding="utf-8") as events:
                for line in events:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("event") != "memory_sample":
                        continue
                    stamp = int(row["monotonic_time_ns"])
                    first_time = stamp if first_time is None else first_time
                    shared_live = int(row.get("shared_live_used_bytes") or 0)
                    warm_allocated = int(row.get("warm_allocated_bytes") or 0)
                    gib = 1024 ** 3
                    residency = row.get("numa_residency") or {}
                    by_node = residency.get("resident_bytes_by_numa", {})
                    evidence = row.get("cgroup_memory") or {}
                    stat = evidence.get("stat") or {}
                    event_counts = evidence.get("events") or {}
                    pressure = evidence.get("pressure") or {}
                    cpu = row.get("cgroup_cpu") or {}
                    writer.writerow({
                        "cgroup_current_bytes": evidence.get("current_bytes"),
                        "swap_current_bytes": evidence.get("swap_current_bytes"),
                        **{f"{key}_bytes": stat.get(key) for key in ("anon", "file", "shmem")},
                        **{key: stat.get(key) for key in ("pgscan", "pgsteal")},
                        **{key: event_counts.get(key) for key in ("oom", "oom_kill")},
                        **{f"psi_{kind}_total_us": (pressure.get(kind) or {}).get("total")
                           for kind in ("some", "full")},
                        "cpu_average_cores": cpu.get("average_cores"),
                        "cpu_utilization_fraction": cpu.get("utilization_fraction"),
                        "cpu_nr_throttled": cpu.get("nr_throttled"),
                        "cpu_throttled_usec": cpu.get("throttled_usec"),
                        **{f"numa_{node}_resident_gib": (
                            f"{by_node[str(node)] / gib:.6f}" if str(node) in by_node else ""
                        ) for node in numa_nodes},
                        "unattributed_gib": (f"{residency['unattributed_bytes'] / gib:.6f}"
                                             if "unattributed_bytes" in residency else ""),
                        "arm_id": arm.get("arm_id"),
                        "policy": (arm.get("policy") or {}).get("name"),
                        "repetition": arm.get("repetition"),
                        "sample": sample,
                        "elapsed_seconds": f"{(stamp - first_time) / 1e9:.6f}",
                        "local_used_gib": f"{int(row.get('local_used_bytes') or 0) / gib:.6f}",
                        "shared_live_used_gib": f"{shared_live / gib:.6f}",
                        "combined_live_used_gib": f"{int(row.get('combined_live_used_bytes') or 0) / gib:.6f}",
                        "warm_allocated_gib": f"{warm_allocated / gib:.6f}",
                        "warm_committed_gib": f"{int(row.get('warm_committed_bytes') or 0) / gib:.6f}",
                        "shared_total_gib": f"{(shared_live + warm_allocated) / gib:.6f}",
                        "host_used_delta_gib": f"{int(row.get('host_used_delta_bytes') or 0) / gib:.6f}",
                        "host_used_gib": f"{int(row.get('host_used_bytes') or 0) / gib:.6f}",
                        "host_available_gib": f"{int(row.get('host_available_bytes') or 0) / gib:.6f}",
                    })
                    sample += 1
    temporary.replace(output)
    return output


def write_run_report_artifacts(run_root: Path) -> tuple[Path, Path]:
    """Write the human report and a plot-ready whole-run memory trace."""
    report = run_root / "report.md"
    temporary = report.with_name(report.name + ".tmp")
    temporary.write_text(render_run_report(run_root), encoding="utf-8")
    temporary.replace(report)
    return report, write_memory_timeseries(run_root)


def render_run_report(run_root: Path) -> str:
    """Render completed arm files; existing runs gain new report fields."""
    rows: list[dict[str, Any]] = []
    for result in _completed_results(run_root):
        event_path = _event_path(run_root, result)
        warm = _warm_metrics(str(event_path) if event_path else None)
        rows.append({"result": result, "warm": warm})
    if not rows:
        summary = run_root / "summary.md"
        if summary.is_file():
            return summary.read_text(encoding="utf-8")
        raise ValueError(f"run has no completed arm records: {run_root}")

    experiment_id = rows[0]["result"].get("experiment_id", run_root.name)
    lines = [
        f"# Experiment {experiment_id}", "",
        f"Run: `{run_root.name}`", "",
    ]
    spec_path = run_root / "experiment.yaml"
    if spec_path.is_file():
        spec = load_experiment(spec_path)
        concurrency = max(spec.execution.concurrency_levels)
        offered = _offered_capacity_text(spec, concurrency)
        resources = spec.resources
        if spec.schema_version == 4:
            from .spec import effective_spec
            lines.extend(['Mechanisms are explicit per arm; a configured mechanism can have zero actions.', '',
                          'Fixed conditions: `' + json.dumps(spec.design['conditions'], sort_keys=True) + '`.', '',
                          '| Arm | Admission | Runtime | Placement | Idle reclaim | Shared memory | Checkpoint | Deployment | Coordination |',
                          '|---|---|---|---|---|---|---|---|---|'])
            for policy in spec.policies:
                m = policy.mechanisms
                lines.append('| ' + ' | '.join((policy.name,
                    m['admission']['tool'],
                    f"memory={m['runtime']['memory_protection']} cpu={m['runtime']['cpu_release_boost']} reclaim={m['runtime']['proactive_memory_reclaim']}",
                    m['placement']['policy'] + f" affinity={m['placement']['template_affinity']}",
                    m['idle_reclaim']['policy'] + '/' + m['idle_reclaim']['scope'],
                    m['shared_memory']['borrow'],
                    '/'.join(m['checkpoint'][key] for key in ('mechanism','storage','residency')),
                    m['deployment']['mode'], m['coordination']['action_selection'])) + ' |')
            lines.append('')
            from types import SimpleNamespace
            resources = SimpleNamespace(**spec.design['environment']['resources'])
        local = resources.local_memory_capacity_mib
        local_text = "unknown" if local is None else f"{local / 1024:g}"
        low = resources.local_memory_low_watermark_mib
        high = resources.local_memory_high_watermark_mib
        borrow = resources.shared_memory_borrow_limit_mib
        watermark_text = (
            f"LOW/HIGH/HARD {low / 1024:g}/{high / 1024:g}/{local / 1024:g} GiB"
            if low is not None and high is not None and local is not None
            else f"{local_text} GiB physical LOCAL cgroup"
        )
        lines.extend([
            f"Resource scope: c{concurrency}, {concurrency * 2} VMs, "
            f"{offered} configured capacity; "
            f"{watermark_text}; "
            f"{resources.warm_memory_capacity_mib / 1024:g} GiB shared pool; "
            f"{(borrow or 0) / 1024:g} GiB maximum live borrow.",
            "",
            ("Deployment and proactive memory-reclaim settings are listed per arm. " if spec.schema_version == 4 else
             f"Lifecycle admission: `{resources.lifecycle_admission}`; proactive cache reclaim: `{resources.proactive_cache_reclaim}`. ")
            + "Configured capacity is offered demand, not measured physical pressure.",
            "",
        ])
    lines.extend([
        "| Policy | c | Status | Sessions | Success | Agents/min | JCT p50/p90/p95/p99 s | "
        "CPU mean/peak cores | CPU throttled s | "
        "Admission blocked s | LOCAL peak GiB | Whole-host peak delta GiB | "
        "HIGH crossings | Above HIGH s/GiB-s | "
        "OOM kills |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for row in rows:
        result = row["result"]
        arm = result.get("arm") or {}
        performance = result.get("performance") or {}
        memory = result.get("memory") or {}
        correctness = result.get("correctness") or {}
        cpu = performance.get("experiment_cpu") or {}
        lines.append(
            f"| {(arm.get('policy') or {}).get('name', 'unknown')} | "
            f"{arm.get('concurrency', 'n/a')} | {result.get('status', 'unknown')} | "
            f"{correctness.get('completed_sessions', 0)}/{arm.get('concurrency', 'n/a')} | "
            f"{_number(performance.get('success_rate'))} | "
            f"{_number(performance.get('agents_per_minute'))} | "
            f"{_number(performance.get('jct_p50_seconds'))}/"
            f"{_number(performance.get('jct_p90_seconds'))}/"
            f"{_number(performance.get('jct_p95_seconds'))}/"
            f"{_number(performance.get('jct_p99_seconds'))} | "
            f"{_number(cpu.get('mean_cores'))}/{_number(cpu.get('peak_interval_cores'))} | "
            f"{_number((cpu.get('throttled_usec_delta') or 0) / 1_000_000)} | "
            f"{_number(performance.get('blocked_admission_seconds'))} | "
            f"{_gib(memory.get('peak_local_tier_bytes', memory.get('peak_used_delta_bytes')))} | "
            f"{_gib(memory.get('host_peak_used_delta_bytes'))} | "
            f"{performance.get('local_high_crossings', 'n/a')} | "
            f"{_number(performance.get('local_high_overshoot_seconds'))}/"
            f"{_gib(performance.get('local_high_overshoot_byte_seconds'))} | "
            f"{memory.get('host_oom_kill_events', 'n/a')} |"
        )

    lines.extend(["", "| Arm | Placement | Memory scheduling | Observed Tool peak/mean | Measured intervals | Offered-to-completion P50/P90/P95/P99 s | Placement wait total s |",
                  "| --- | --- | --- | ---: | ---: | ---: | ---: |"])
    for row in rows:
        result = row['result']
        arm = result.get('arm') or {}
        concurrency = result.get('performance', {}).get('observed_tool_concurrency', {})
        lines.append(f"| {arm.get('arm_id', 'n/a')} | {(arm.get('execution') or {}).get('placement_policy', 'round_robin')} | "
                     f"{(arm.get('resources') or {}).get('memory_scheduling', 'static')} | "
                     f"{_number(concurrency.get('peak'))}/{_number(concurrency.get('mean_over_tool_window'))} | "
                     f"{concurrency.get('interval_count', 'n/a')} | "
                     f"{_number(result.get('performance', {}).get('offered_to_completion_p50_seconds'))}/"
                     f"{_number(result.get('performance', {}).get('offered_to_completion_p90_seconds'))}/"
                     f"{_number(result.get('performance', {}).get('offered_to_completion_p95_seconds'))}/"
                     f"{_number(result.get('performance', {}).get('offered_to_completion_p99_seconds'))} | "
                     f"{_number(result.get('performance', {}).get('placement_wait_total_seconds'))} |")
    lines.extend(["", "Tool overlap includes admitted backend maintenance and uses completed operation intervals; "
                  "`performance.observed_agent_tool_concurrency` separately measures Agent Tool operations. "
                  "Cross-VM overlap requires synchronized Runtime VM clocks. "
                  "Incomplete failed sessions are not evidence of zero Tool concurrency."])
    lines.extend([
        "", "| Policy | Pause/restore count | Pause/restore s | Borrow count/s | Return/released count/s | "
        "Shared live/total peak GiB | WARM transferred GiB / private Delta GiB | Unique Base backing GiB/refs | WARM peak committed GiB | "
        "Prediction coverage | Prediction sources | "
        "Prediction error p90 MiB | ID join | Lost events | Cleanup |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---|",
    ])
    for row in rows:
        result = row["result"]
        arm = result.get("arm") or {}
        performance = result.get("performance") or {}
        memory = result.get("memory") or {}
        correctness = result.get("correctness") or {}
        warm = row["warm"]
        lines.append(
            f"| {(arm.get('policy') or {}).get('name', 'unknown')} | "
            f"{performance.get('pause_count', 0)}/{performance.get('resume_count', 0)} | "
            f"{_number(performance.get('pause_service_seconds'))}/"
            f"{_number(performance.get('resume_service_seconds'))} | "
            f"{performance.get('shared_memory_borrow_count', 'n/a')}/"
            f"{_number(performance.get('shared_memory_borrow_service_seconds'))} | "
            f"{performance.get('shared_memory_return_count', 'n/a')}/"
            f"{performance.get('shared_memory_return_release_count', 'n/a')}/"
            f"{_number(performance.get('shared_memory_return_service_seconds'))} | "
            f"{_gib(memory.get('peak_shared_live_bytes'))}/"
            f"{_gib(memory.get('peak_shared_total_bytes'))} | "
            f"{_gib(warm['transferred_bytes'])}/{_gib(warm['private_delta_bytes'])} | "
            f"{_gib(warm['unique_base_backing_bytes'])}/{warm['base_reference_count']} | "
            f"{_gib(warm['peak_committed_bytes'])} | "
            f"{_number(performance.get('prediction_coverage_fraction'))} | "
            f"{_distribution(performance.get('prediction_source_distribution'))} | "
            f"{_number(performance.get('prediction_error_p90_mib'))} | "
            f"{_number(correctness.get('native_tool_exact_id_join_rate'))} | "
            f"{correctness.get('native_tool_telemetry_loss_total', 'n/a')} | "
            f"{correctness.get('cleanup_verified', False)} |"
        )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        result = row["result"]
        # Failed/cancelled arms remain visible above but cannot improve a
        # performance mean by ending early. Only repetition/identity may vary.
        arm = result.get('arm') or {}
        condition = {k: v for k,v in arm.items() if k not in {'arm_id','spec_digest','repetition'}}
        encoded = json.dumps(condition, sort_keys=True, separators=(',', ':'))
        policy = (arm.get('policy') or {}).get('name','unknown')
        label = (f"{policy} / c={arm.get('concurrency','?')} / "
                 f"{(arm.get('case') or {}).get('case_id','mixed')} / "
                 f"{(arm.get('policy') or {}).get('runtime_control','unspecified')} / "
                 f"{hashlib.sha256(encoded.encode()).hexdigest()[:8]}")
        grouped.setdefault((label, encoded), []).append(result)
    if any(len(values) > 1 for values in grouped.values()):
        lines.extend([
            "", "## Cross-repetition summary", "",
            "Performance means are conditional on succeeded trials; all trial outcomes are counted. Values are mean +/- sample SD; raw "
            "per-arm values remain above.", "",
            "| Policy | Succeeded / offered trials | Agents/min | Mean within-trial JCT p95 s | Admission blocked s | "
            "LOCAL peak GiB | Whole-host peak delta GiB | Above HIGH s |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for (policy, _), results in grouped.items():
            def values(section: str, key: str, divisor: float = 1.0) -> list[float]:
                found = []
                for item in results:
                    if item.get('status') != 'succeeded':
                        continue
                    value = (item.get(section) or {}).get(key)
                    if value is not None:
                        found.append(float(value) / divisor)
                return found

            lines.append(
                f"| {policy} | {sum(r.get('status') == 'succeeded' for r in results)} / {len(results)} | "
                f"{_mean_sd_text(values('performance', 'agents_per_minute'))} | "
                f"{_mean_sd_text(values('performance', 'jct_p95_seconds'))} | "
                f"{_mean_sd_text(values('performance', 'blocked_admission_seconds'))} | "
                f"{_mean_sd_text(values('memory', 'peak_local_tier_bytes'), 1024 ** 3)} | "
                f"{_mean_sd_text(values('memory', 'host_peak_used_delta_bytes'), 1024 ** 3)} | "
                f"{_mean_sd_text(values('performance', 'local_high_overshoot_seconds'))} |"
            )
    lines.extend(['', '## Trial outcomes', '',
        'Counts use all offered trials. OOM observations can overlap timeouts or other failures; '
        'missing counters remain unknown. Infrastructure or validation failures invalidate a performance comparison.', '',
        '| Policy | Trials | Succeeded | Timed out | Failed | Cancelled / rejected | Observed OOM / unknown |',
        '| --- | ---: | ---: | ---: | ---: | ---: | --- |'])
    for (policy, _), results in grouped.items():
        counts = {status: sum(r.get('status') == status for r in results)
                  for status in ('succeeded', 'timed_out', 'failed', 'cancelled', 'rejected')}
        ooms = sum((r.get('memory', {}).get('host_oom_kill_events') or 0) > 0
                   or r.get('failure_category') == 'oom' for r in results)
        unknown = sum(r.get('memory', {}).get('host_oom_kill_events') is None
                      and r.get('failure_category') != 'oom' for r in results)
        lines.append(f"| {policy} | {len(results)} | {counts['succeeded']} | {counts['timed_out']} | "
                     f"{counts['failed']} | {counts['cancelled'] + counts['rejected']} | {ooms} / {unknown} |")
    lines.extend(['', '## All-trial priority outcomes', '',
        'Latency is conditional on completion. P95 requires at least 20 completed samples; '
        'its 95% bootstrap interval resamples whole trials and requires at least five trials. '
        'Failed, timed out and interrupted trials remain in the offered denominator. '
        'Completion counts include only verified session results; a killed worker may not report partial completions.', '',
        '| Policy | Priority | Verified completed / offered | P95 s | 95% interval s |',
        '| --- | --- | --- | ---: | --- |'])
    for (policy, _), results in grouped.items():
        for priority in ('high', 'low'):
            summary = priority_summary(results, priority)
            interval = summary['p95_interval']
            lines.append(f"| {policy} | {priority} | {summary['completed']} / {summary['offered']} | "
                         f"{_number(summary['p95'])} | "
                         f"{_number(interval[0])}..{_number(interval[1])} |")
    if any(row['result'].get('performance', {}).get('runtime_memory_control') for row in rows):
        lines.extend(['', '## Runtime congestion control', '',
            '| Arm | Backend | Activations | Max poll gap s | HIGH completed / offered | LOW completed / offered | HIGH completed samples | LOW completed samples |',
            '| --- | --- | ---: | ---: | --- | --- | ---: | ---: |'])
        for row in rows:
            result=row['result']; performance=result.get('performance') or {}
            runtime=performance.get('runtime_memory_control') or {}
            priorities=performance.get('priority_sessions') or {}
            high,low=priorities.get('high',{}),priorities.get('low',{})
            lines.append(f"| {(result.get('arm') or {}).get('arm_id')} | {runtime.get('backend','off')} | "
                f"{(runtime.get('upstream') or {}).get('activations','n/a')} | {_number(runtime.get('max_poll_gap_seconds'))} | "
                f"{high.get('completed',0)} / {high.get('offered','?')} | {low.get('completed',0)} / {low.get('offered','?')} | "
                f"{len(high.get('durations_seconds', []))} | {len(low.get('durations_seconds', []))} |")
    lines.extend([
        "",
        "Configured VM capacity, admission reservations, predictions, LOCAL physical "
        "use, live shared-NUMA borrowing, and WARM snapshot bytes are distinct quantities. "
        "Crossing HIGH is an observed control event, not a failed arm. Policy OOMs and timeouts are outcomes, not samples to discard. Crossing "
        "the shared-pool capacity, validation failures, telemetry loss, or unverified "
        "cleanup invalidate performance comparison. One repetition does not estimate "
        "run-to-run uncertainty.",
    ])
    if any(row["result"].get("arm", {}).get("resources", {}).get("compute_nodes") for row in rows):
        traffic_available = any(
            (row["result"].get("memory") or {}).get("cross_numa_traffic_bytes")
            is not None for row in rows
        )
        lines.extend([
            "",
            "Cross-NUMA traffic bytes: " + (
                "measured in at least one arm."
                if traffic_available else
                "unavailable; no qualified platform-specific host uncore PMU event "
                "was configured. `numa_allocation_event_deltas` counts kernel "
                "allocation events in pages and is not a traffic-byte measurement."
            ),
        ])
    if any(row["result"].get("arm", {}).get("resources", {}).get("compute_nodes") for row in rows):
        lines.extend(["", "## Compute nodes", "",
            "Each node has independent admission and LOW/HIGH/HARD. Overshoot seconds in the aggregate table sum node-seconds. "
            "Node peaks include conservatively charged unattributed bytes; they must not be summed as a simultaneous physical peak.", "",
            "| Arm | Node | NUMA | CPUs | LOW/HIGH/HARD GiB | Peak local GiB | HIGH crossings | Borrows | Verified placements |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"])
        for row in rows:
            result = row["result"]
            for node_id, node in result.get("performance", {}).get("compute_nodes", {}).items():
                w = node.get("watermarks") or {}
                r = node["resources"]
                placements = node.get("placements") or []
                cpus = placements[0].get("effective_cpus") if placements else "n/a"
                lines.append(f"| {result['arm']['arm_id']} | {node_id} | {r.get('local_numa_node')} | {cpus} | "
                    f"{_gib(w.get('low'))}/{_gib(w.get('high'))}/{_gib(w.get('hard'))} | "
                    f"{_gib(w.get('peak_local_bytes'))} | {w.get('high_crossings', 'n/a')} | "
                    f"{w.get('borrow_count', 'n/a')} | {len(placements)} |")
        lines.extend(["", "CPU/memory bindings are verified after VM creation and restore, before further worker dispatch. "
            "The supported priority-domain path prepares the cpuset before VMM allocation; "
            "check runtime_memory_placement events for preallocation enforcement and vCPU affinity. "
            "They do not prove that existing pages migrated. Per-NUMA physical samples are in memory-timeseries.csv; "
            "session placement and cgroup evidence are in each arm's JSON.", ""])
    return "\n".join(lines) + "\n"
