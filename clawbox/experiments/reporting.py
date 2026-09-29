"""Human-readable, auditable summary of a completed standalone run."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import statistics
from typing import Any

from .spec import load_experiment


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
        "peak_allocated_bytes": 0,
        "peak_committed_bytes": 0,
    }
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
                    writer.writerow({
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
        offered = concurrency * (
            spec.runtime.memory_mib + spec.sandbox.memory_mib
        ) / 1024
        resources = spec.resources
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
            f"{offered:g} GiB configured capacity; "
            f"{watermark_text}; "
            f"{resources.warm_memory_capacity_mib / 1024:g} GiB shared pool; "
            f"{(borrow or 0) / 1024:g} GiB maximum live borrow.",
            "",
        ])
    lines.extend([
        "| Policy | c | Status | Sessions | Agents/min | JCT p50/p95 s | "
        "Admission blocked s | LOCAL peak GiB | Whole-host peak delta GiB | "
        "HIGH crossings | Above HIGH s/GiB-s | "
        "OOM kills |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for row in rows:
        result = row["result"]
        arm = result.get("arm") or {}
        performance = result.get("performance") or {}
        memory = result.get("memory") or {}
        correctness = result.get("correctness") or {}
        lines.append(
            f"| {(arm.get('policy') or {}).get('name', 'unknown')} | "
            f"{arm.get('concurrency', 'n/a')} | {result.get('status', 'unknown')} | "
            f"{correctness.get('completed_sessions', 0)}/{arm.get('concurrency', 'n/a')} | "
            f"{_number(performance.get('agents_per_minute'))} | "
            f"{_number(performance.get('jct_p50_seconds'))}/"
            f"{_number(performance.get('jct_p95_seconds'))} | "
            f"{_number(performance.get('blocked_admission_seconds'))} | "
            f"{_gib(memory.get('peak_local_tier_bytes', memory.get('peak_used_delta_bytes')))} | "
            f"{_gib(memory.get('host_peak_used_delta_bytes'))} | "
            f"{performance.get('local_high_crossings', 'n/a')} | "
            f"{_number(performance.get('local_high_overshoot_seconds'))}/"
            f"{_gib(performance.get('local_high_overshoot_byte_seconds'))} | "
            f"{memory.get('host_oom_kill_events', 'n/a')} |"
        )

    lines.extend([
        "", "| Policy | Pause/restore count | Pause/restore s | Borrow count/s | "
        "Shared live/total peak GiB | WARM transferred GiB | WARM peak committed GiB | "
        "Prediction coverage | Prediction sources | "
        "Prediction error p90 MiB | ID join | Lost events | Cleanup |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---|",
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
            f"{_gib(memory.get('peak_shared_live_bytes'))}/"
            f"{_gib(memory.get('peak_shared_total_bytes'))} | "
            f"{_gib(warm['transferred_bytes'])} | "
            f"{_gib(warm['peak_committed_bytes'])} | "
            f"{_number(performance.get('prediction_coverage_fraction'))} | "
            f"{_distribution(performance.get('prediction_source_distribution'))} | "
            f"{_number(performance.get('prediction_error_p90_mib'))} | "
            f"{_number(correctness.get('native_tool_exact_id_join_rate'))} | "
            f"{correctness.get('native_tool_telemetry_loss_total', 'n/a')} | "
            f"{correctness.get('cleanup_verified', False)} |"
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        result = row["result"]
        policy = str(((result.get("arm") or {}).get("policy") or {}).get("name", "unknown"))
        grouped.setdefault(policy, []).append(result)
    if any(len(values) > 1 for values in grouped.values()):
        lines.extend([
            "", "## Cross-repetition summary", "",
            "Values are arithmetic mean +/- sample SD across completed repetitions; raw "
            "per-arm values remain above.", "",
            "| Policy | n | Agents/min | JCT p95 s | Admission blocked s | "
            "LOCAL peak GiB | Whole-host peak delta GiB | Above HIGH s |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for policy, results in grouped.items():
            def values(section: str, key: str, divisor: float = 1.0) -> list[float]:
                found = []
                for item in results:
                    value = (item.get(section) or {}).get(key)
                    if value is not None:
                        found.append(float(value) / divisor)
                return found

            lines.append(
                f"| {policy} | {len(results)} | "
                f"{_mean_sd_text(values('performance', 'agents_per_minute'))} | "
                f"{_mean_sd_text(values('performance', 'jct_p95_seconds'))} | "
                f"{_mean_sd_text(values('performance', 'blocked_admission_seconds'))} | "
                f"{_mean_sd_text(values('memory', 'peak_local_tier_bytes'), 1024 ** 3)} | "
                f"{_mean_sd_text(values('memory', 'host_peak_used_delta_bytes'), 1024 ** 3)} | "
                f"{_mean_sd_text(values('performance', 'local_high_overshoot_seconds'))} |"
            )
    lines.extend([
        "",
        "Configured VM capacity, admission reservations, predictions, LOCAL physical "
        "use, live shared-NUMA borrowing, and WARM snapshot bytes are distinct quantities. "
        "Crossing HIGH is an observed control event, not a failed arm. OOMs, crossing "
        "the shared-pool capacity, validation failures, telemetry loss, or unverified "
        "cleanup invalidate performance comparison. One repetition does not estimate "
        "run-to-run uncertainty.",
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
            "Boot and autonomous guest resume may execute before binding. "
            "They do not prove that existing pages migrated. Per-NUMA physical samples are in memory-timeseries.csv; "
            "session placement and cgroup evidence are in each arm's JSON.", ""])
    return "\n".join(lines) + "\n"
