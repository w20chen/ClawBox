"""Fail-closed paper tables for registered replay and density suites."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .suite import SUITE_METRICS


MAIN_BASELINES = (
    "replay-fixed-resident",
    "replay-fixed-snapshot",
    "replay-fixed2-resident",
    "replay-fixed2-snapshot",
    "replay-p90_static-resident",
    "replay-p90_static-snapshot",
)
DENSITY_BASELINES = (
    "replay-fixed2-resident",
    "replay-fixed2-snapshot",
)

METRICS = tuple(SUITE_METRICS)


def _number(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def load_complete_suite(root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Load a complete suite, rejecting any partial, divergent, or incorrect arm."""
    summary_path = root / "suite-summary.json"
    measurements_path = root / "measurements.csv"
    if not summary_path.is_file() or not measurements_path.is_file():
        raise ValueError(f"{root}: suite-summary.json and measurements.csv are required")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("all_successful") is not True:
        raise ValueError(f"{root}: suite is not globally successful")
    runs = summary.get("runs") or []
    if not runs or any(
        int(run.get("status", 1)) != 0 or run.get("final_state_equal") is not True
        for run in runs
    ):
        raise ValueError(f"{root}: suite contains an incomplete or divergent block")
    with measurements_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"{root}: measurements.csv is empty")
    expected_rows = sum(
        int(group.get("runs", 0))
        for run in runs for group in (run.get("groups") or {}).values()
    )
    if expected_rows <= 0 or len(rows) != expected_rows:
        raise ValueError(
            f"{root}: measurements.csv has {len(rows)} arms; expected {expected_rows}"
        )
    seen: set[tuple[str, ...]] = set()
    for index, row in enumerate(rows, 2):
        identity = tuple(
            row.get(field, "")
            for field in ("workload", "concurrency", "baseline", "repetition", "inference_backend")
        )
        if identity in seen:
            raise ValueError(f"{measurements_path}:{index}: duplicate arm identity {identity}")
        seen.add(identity)
        if (
            int(row.get("failure_count") or -1) != 0
            or int(row.get("sessions_completed") or -1)
            != int(row.get("sessions_requested") or -2)
            or row.get("block_final_state_equal", "").lower() != "true"
            or _number(row.get("correctness_pass_fraction")) != 1.0
            or _number(row.get("throughput_correct_tasks_per_minute")) is None
        ):
            raise ValueError(f"{measurements_path}:{index}: arm failed integrity/correctness gate")
    return summary, rows


def _macro_mean(summary: dict[str, Any], concurrency: int, baseline: str, metric: str) -> float:
    try:
        value = summary["macro_statistics"][f"c{concurrency:02d}/{baseline}"][metric]["mean"]
    except KeyError as exc:
        raise ValueError(
            f"missing macro statistic c{concurrency:02d}/{baseline}/{metric}"
        ) from exc
    if value is None:
        raise ValueError(f"macro statistic c{concurrency:02d}/{baseline}/{metric} is null")
    return float(value)


def _configured_tool_memory_mib(
    summary: dict[str, Any], concurrency: int, baseline: str,
) -> int:
    values = {
        int(value)
        for run in summary.get("runs", [])
        if int(run.get("concurrency", -1)) == concurrency
        for value in (run.get("groups", {}).get(baseline, {}).get(
            "configured_tool_memory_mib", []
        ))
    }
    if len(values) != 1:
        raise ValueError(
            f"expected one configured Tool-memory value for c{concurrency:02d}/{baseline}; "
            f"found {sorted(values)}"
        )
    return values.pop()


def _group_row(summary: dict[str, Any], concurrency: int, baseline: str) -> dict[str, Any]:
    raw = {metric: _macro_mean(summary, concurrency, baseline, metric) for metric in METRICS}
    return {
        "concurrency": concurrency,
        "baseline": baseline,
        "configured_tool_memory_mib": _configured_tool_memory_mib(
            summary, concurrency, baseline
        ),
        "correct_tasks_per_min": raw["throughput_correct_tasks_per_minute"],
        "completed_tasks_per_min": raw["throughput_tasks_per_minute"],
        "steps_per_min": raw["throughput_steps_per_minute"],
        "wall_s": raw["wall_s"],
        "correctness_percent": 100.0 * raw["correctness_pass_fraction"],
        "mean_rss_gib": raw["mean_firecracker_rss_bytes"] / 2**30,
        "p95_rss_gib": raw["p95_firecracker_rss_bytes"] / 2**30,
        "peak_rss_gib": raw["peak_firecracker_rss_bytes"] / 2**30,
        "rss_time_gib_hours": raw["firecracker_rss_time_byte_seconds"] / 2**30 / 3600,
        "mean_numa_memory_delta_gib": raw["mean_numa_memory_delta_bytes"] / 2**30,
        "peak_numa_memory_delta_gib": raw["peak_numa_memory_delta_bytes"] / 2**30,
        "mean_cgroup_memory_delta_gib": raw["mean_cgroup_memory_delta_bytes"] / 2**30,
        "peak_cgroup_memory_delta_gib": raw["peak_cgroup_memory_delta_bytes"] / 2**30,
        "peak_resident_vms": raw["peak_resident_vms"],
        "checkpoint_cycles": raw["checkpoint_cycles"],
        "vm_snapshot_operations": raw["vm_snapshot_operations"],
        "vm_restore_operations": raw["vm_restore_operations"],
        "admission_wait_total_s": raw["admission_wait_s"],
        "mean_admission_wait_s": raw["mean_admission_wait_event_s"],
        "p95_admission_wait_s": raw["p95_admission_wait_event_s"],
        "p50_session_s": raw["p50_session_wall_s"],
        "p95_session_s": raw["p95_session_wall_s"],
        "p99_session_s": raw["p99_session_wall_s"],
        "max_admission_wait_s": raw["max_admission_wait_event_s"],
        "snapshot_service_s": raw["checkpoint_snapshot_service_s"],
        "restore_service_s": raw["checkpoint_restore_service_s"],
        "snapshot_allocated_gib": raw["snapshot_allocated_bytes"] / 2**30,
        "raw_metrics": raw,
    }


def _contrast_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    selected_metrics = (
        "throughput_correct_tasks_per_minute",
        "throughput_steps_per_minute",
        "wall_s",
        "firecracker_rss_time_byte_seconds",
        "mean_firecracker_rss_bytes",
        "p95_session_wall_s",
    )
    rows: list[dict[str, Any]] = []
    for comparison, metrics in sorted((summary.get("paired_contrasts") or {}).items()):
        for metric in selected_metrics:
            payload = metrics.get(metric)
            if not payload:
                continue
            percent = payload.get("percent_effect_statistics") or {}
            mean = payload.get("mean")
            half_width = payload.get("ci95_half_width")
            rows.append({
                "comparison": comparison,
                "metric": metric,
                "independent_task_n": int(payload.get("n", 0)),
                "paired_arm_count": int(payload.get("pair_count", 0)),
                "mean_absolute_effect": mean,
                "mean_percent_effect": percent.get("mean"),
                "ci95_low": None if half_width is None else float(mean) - float(half_width),
                "ci95_high": None if half_width is None else float(mean) + float(half_width),
            })
    return rows


def build_report(main_root: Path, density_root: Path) -> dict[str, Any]:
    main, main_measurements = load_complete_suite(main_root)
    density, density_measurements = load_complete_suite(density_root)
    main_levels = [int(value) for value in main["concurrency_levels"]]
    density_levels = [int(value) for value in density["concurrency_levels"]]
    for level in main_levels:
        for baseline in MAIN_BASELINES:
            if f"c{level:02d}/{baseline}" not in main.get("macro_statistics", {}):
                raise ValueError(f"main suite missing c{level:02d}/{baseline}")
    for level in density_levels:
        for baseline in DENSITY_BASELINES:
            if f"c{level:02d}/{baseline}" not in density.get("macro_statistics", {}):
                raise ValueError(f"density suite missing c{level:02d}/{baseline}")
    units = sorted(set(main.get("independent_units") or []) | set(density.get("independent_units") or []))
    return {
        "schema_version": 1,
        "inference": {
            "sampling_unit": "independent SWE task",
            "independent_task_n": len(units),
            "independent_units": units,
            "ci95_estimable": len(units) >= 2,
            "note": (
                "Trajectories and repetitions are robustness repeats, not independent samples."
            ),
        },
        "main": {
            "root": str(main_root),
            "block_count": len(main["runs"]),
            "arm_count": len(main_measurements),
            "rows": [
                _group_row(main, level, baseline)
                for level in main_levels for baseline in MAIN_BASELINES
            ],
            "paired_contrasts": _contrast_rows(main),
            "artifact_provenance": main.get("artifact_provenance"),
            "prediction_provenance": main.get("prediction_provenance"),
        },
        "density": {
            "root": str(density_root),
            "block_count": len(density["runs"]),
            "arm_count": len(density_measurements),
            "rows": [
                _group_row(density, level, baseline)
                for level in density_levels for baseline in DENSITY_BASELINES
            ],
            "paired_contrasts": _contrast_rows(density),
            "artifact_provenance": density.get("artifact_provenance"),
        },
    }


def _fmt(value: Any, digits: int = 3) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def markdown_report(report: dict[str, Any]) -> str:
    inference = report["inference"]
    lines = [
        "# Registered replay baseline results",
        "",
        (
            f"Independent task n = {inference['independent_task_n']}; "
            f"95% CI estimable: {'yes' if inference['ci95_estimable'] else 'no'}. "
            f"{inference['note']}"
        ),
        "",
        "## Main six-arm sweep",
        "",
        "| c | Baseline | Tool MiB | Correct tasks/min | Steps/min | Wall s | "
        "Mean/peak RSS GiB | RSS-time GiB-h | P95 session s | Checkpoint cycles | Correct |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["main"]["rows"]:
        lines.append(
            f"| {row['concurrency']} | {row['baseline']} | "
            f"{row['configured_tool_memory_mib']} | "
            f"{_fmt(row['correct_tasks_per_min'])} | {_fmt(row['steps_per_min'])} | "
            f"{_fmt(row['wall_s'], 2)} | {_fmt(row['mean_rss_gib'])}/"
            f"{_fmt(row['peak_rss_gib'])} | "
            f"{_fmt(row['rss_time_gib_hours'])} | {_fmt(row['p95_session_s'], 2)} | "
            f"{_fmt(row['checkpoint_cycles'], 1)} | "
            f"{_fmt(row['correctness_percent'], 1)}% |"
        )
    lines.extend([
        "",
        "## Configured-budget density sweep",
        "",
        "| c | Baseline | Correct tasks/min | Steps/min | Wall s | Mean/peak RSS GiB | "
        "Peak resident VMs | P95 session s | P95/max wait s | Snapshot GiB |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in report["density"]["rows"]:
        lines.append(
            f"| {row['concurrency']} | {row['baseline']} | "
            f"{_fmt(row['correct_tasks_per_min'])} | {_fmt(row['steps_per_min'])} | "
            f"{_fmt(row['wall_s'], 2)} | {_fmt(row['mean_rss_gib'])}/"
            f"{_fmt(row['peak_rss_gib'])} | {_fmt(row['peak_resident_vms'], 1)} | "
            f"{_fmt(row['p95_session_s'], 2)} | {_fmt(row['p95_admission_wait_s'], 2)}/"
            f"{_fmt(row['max_admission_wait_s'], 2)} | "
            f"{_fmt(row['snapshot_allocated_gib'])} |"
        )
    lines.extend([
        "",
        "## Paired effects",
        "",
        "Positive percentages mean treatment > control; interpret direction by metric.",
        "",
        "| Suite | Comparison | Metric | Pairs | Task n | Mean effect | Mean % | 95% CI |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ])
    for suite_name in ("main", "density"):
        for row in report[suite_name]["paired_contrasts"]:
            interval = (
                "not estimable" if row["ci95_low"] is None
                else f"[{_fmt(row['ci95_low'])}, {_fmt(row['ci95_high'])}]"
            )
            lines.append(
                f"| {suite_name} | {row['comparison']} | {row['metric']} | "
                f"{row['paired_arm_count']} | {row['independent_task_n']} | "
                f"{_fmt(row['mean_absolute_effect'])} | {_fmt(row['mean_percent_effect'])} | "
                f"{interval} |"
            )
    return "\n".join(lines) + "\n"
