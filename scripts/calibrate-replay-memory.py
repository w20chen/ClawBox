#!/usr/bin/env python3
"""Derive fail-closed replay memory reservations from independent pilot runs."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


MIB = 1024 * 1024


def quantile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(int(value) for value in values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[min(index, len(ordered) - 1)]


def _lease_growth(summary: dict[str, Any], request_class: str, key: str) -> list[int]:
    admission = summary.get("atomic_memory_admission") or {}
    return [
        int(item[key])
        for item in admission.get("completed_leases", [])
        if item.get("request_class") == request_class and item.get(key) is not None
    ]


def _restore_transient_growth(
    summary: dict[str, Any], peak_key: str, increment_key: str,
) -> list[int]:
    """Return restore growth beyond the pre-eviction resident-set reservation."""
    configured_transient = max(
        0, int(summary.get("restore_transient_headroom_mib") or 0) * MIB,
    )
    values = []
    admission = summary.get("atomic_memory_admission") or {}
    for item in admission.get("completed_leases", []):
        if item.get("request_class") != "restore":
            continue
        peak = item.get(peak_key)
        increment = item.get(increment_key)
        if peak is None or increment is None:
            continue
        base_restore = max(0, int(increment) - configured_transient)
        values.append(max(0, int(peak) - base_restore))
    return values


def _checkpoint_growth(summary: dict[str, Any], scope: str) -> list[int]:
    values: list[int] = []
    for session in summary.get("sessions", []):
        for event in session.get("checkpoint_reclamation_events", []):
            metrics = event.get(f"{scope}_checkpoint_metrics")
            if isinstance(metrics, dict):
                value = metrics.get("cgroup_memory_transient_growth_bytes")
                if value is not None:
                    values.append(int(value))
    return values


def _checkpoint_parent_growth(summary: dict[str, Any]) -> list[int]:
    values: list[int] = []
    for session in summary.get("sessions", []):
        for event in session.get("checkpoint_reclamation_events", []):
            local = []
            for scope in ("runtime", "tool"):
                metrics = event.get(f"{scope}_checkpoint_metrics")
                if isinstance(metrics, dict) and metrics.get(
                    "cgroup_memory_transient_growth_bytes"
                ) is not None:
                    local.append(int(metrics["cgroup_memory_transient_growth_bytes"]))
            if local:
                values.append(max(local))
    return values


def _aggregate_positive_residual_peak(summary: dict[str, Any]) -> int | None:
    intervals = []
    for session in summary.get("sessions", []):
        for row in session.get("tool_working_sets", []):
            prediction = row.get("predicted_command_memory_p90_mib")
            start = row.get("first_execution_started_unix_s")
            end = row.get("last_execution_completed_unix_s")
            actual = row.get("actual_command_peak_memory_bytes")
            if None in (prediction, start, end, actual):
                continue
            residual = max(0, int(actual) - math.ceil(float(prediction) * MIB))
            intervals.append((float(start), float(end), residual))
    if not intervals:
        return None
    points = sorted({value for start, end, _residual in intervals for value in (start, end)})
    return max(
        sum(residual for start, end, residual in intervals if start <= point <= end)
        for point in points
    )


def calibrate(summaries: list[dict[str, Any]], quantile_fraction: float) -> dict[str, Any]:
    boot_parent = [
        value for summary in summaries
        for value in _lease_growth(summary, "boot", "parent_peak_growth_bytes")
    ]
    boot_tool = [
        value for summary in summaries
        for value in _lease_growth(summary, "boot", "tool_peak_growth_bytes")
    ]
    restore_parent = [
        value for summary in summaries
        for value in _restore_transient_growth(
            summary, "parent_peak_growth_bytes", "parent_increment_bytes",
        )
    ]
    restore_tool = [
        value for summary in summaries
        for value in _restore_transient_growth(
            summary, "tool_peak_growth_bytes", "tool_increment_bytes",
        )
    ]
    checkpoint_tool = [
        value for summary in summaries
        for value in _checkpoint_growth(summary, "tool")
    ]
    checkpoint_parent = [
        value for summary in summaries
        for value in _checkpoint_parent_growth(summary)
    ]
    aggregate_residual = [
        value for summary in summaries
        if (value := _aggregate_positive_residual_peak(summary)) is not None
    ]

    def recommendation(values: list[int]) -> dict[str, Any]:
        selected = quantile(values, quantile_fraction)
        return {
            "samples": len(values),
            "quantile_bytes": selected,
            "recommended_mib": (
                None if selected is None else max(1, math.ceil(selected / MIB))
            ),
            "max_bytes": max(values, default=None),
        }

    return {
        "schema_version": 1,
        "quantile": quantile_fraction,
        "independent_run_count": len(summaries),
        "boot_parent": recommendation(boot_parent),
        "boot_tool": recommendation(boot_tool),
        "restore_transient_parent": recommendation(restore_parent),
        "restore_transient_tool": recommendation(restore_tool),
        "checkpoint_parent": recommendation(checkpoint_parent),
        "checkpoint_tool": recommendation(checkpoint_tool),
        "aggregate_positive_prediction_residual": recommendation(aggregate_residual),
        "formal_ready": all(
            values for values in (
                boot_parent, boot_tool, restore_parent, restore_tool,
                checkpoint_parent, checkpoint_tool, aggregate_residual,
            )
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("summaries", nargs="+", type=Path)
    parser.add_argument("--quantile", type=float, default=0.99)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 < args.quantile <= 1:
        parser.error("--quantile must be in (0, 1]")
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in args.summaries]
    result = calibrate(payloads, args.quantile)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if result["formal_ready"] else 2)


if __name__ == "__main__":
    main()
