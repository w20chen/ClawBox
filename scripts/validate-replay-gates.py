#!/usr/bin/env python3
"""Fail-closed validity gates for a direct-Firecracker replay arm."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def _number(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def validate_summary(summary: dict[str, Any], *, formal: bool) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"gate": name, "passed": bool(passed), "detail": detail})

    requested = int(summary.get("sessions_requested") or 0)
    completed = int(summary.get("sessions_completed") or 0)
    check(
        "all_sessions_complete",
        requested > 0 and completed == requested and not summary.get("failures"),
        f"completed={completed}, requested={requested}, failures={len(summary.get('failures') or [])}",
    )
    check(
        "correctness",
        summary.get("correctness_evaluated") is True
        and _number(summary, "correctness_pass_fraction") == 1.0,
        f"pass_fraction={summary.get('correctness_pass_fraction')!r}",
    )
    check(
        "exactly_once_model",
        int(summary.get("duplicate_model_executions") or 0) == 0
        and int(summary.get("model_gateway_delivery_failures") or 0) == 0
        and int(summary.get("model_production_attempts") or 0)
        == int(summary.get("model_steps_completed") or 0)
        and int(summary.get("model_gateway_responses_delivered") or 0)
        == int(summary.get("model_steps_completed") or 0),
        (
            f"duplicate_executions={summary.get('duplicate_model_executions')}, "
            f"delivery_failures={summary.get('model_gateway_delivery_failures')}, "
            f"production={summary.get('model_production_attempts')}, "
            f"delivered={summary.get('model_gateway_responses_delivered')}, "
            f"steps={summary.get('model_steps_completed')}"
        ),
    )
    check(
        "replay_input_identity",
        summary.get("inference") != "replay"
        or summary.get("replay_input_validation_complete") is True,
        (
            f"inference={summary.get('inference')!r}, "
            f"complete={summary.get('replay_input_validation_complete')!r}"
        ),
    )
    check(
        "tool_execution_count",
        int(summary.get("tool_execution_count_mismatch_events") or 0) == 0,
        f"mismatches={summary.get('tool_execution_count_mismatch_events')}",
    )
    check(
        "no_oom",
        int(summary.get("host_oom_kill_events") or 0) == 0
        and int(summary.get("tenant_guest_oom_events") or 0) == 0,
        (
            f"host={summary.get('host_oom_kill_events')}, "
            f"guest={summary.get('tenant_guest_oom_events')}"
        ),
    )
    swap = _number(summary, "peak_vm_pool_swap_current_bytes")
    check("no_swap", swap == 0, f"peak_swap_bytes={swap!r}")
    off_numa = _number(summary, "max_vm_pool_off_numa_ratio")
    check(
        "numa_local",
        off_numa is not None and off_numa <= 0.001,
        f"off_numa_ratio={off_numa!r}",
    )
    kernel_peak = _number(summary, "kernel_peak_vm_pool_memory_bytes")
    check(
        "kernel_memory_peak_available",
        kernel_peak is not None and kernel_peak > 0,
        f"kernel_peak_bytes={kernel_peak!r}",
    )

    reclamation = summary.get("reclamation_policy")
    if reclamation in {"checkpoint", "hybrid"}:
        cycles = int(summary.get("checkpoint_cycles") or 0)
        check("checkpoint_observed", cycles > 0, f"cycles={cycles}")
        check(
            "checkpoint_io_observed",
            int(summary.get("checkpoint_process_write_bytes") or 0) > 0
            and int(summary.get("checkpoint_snapshot_logical_bytes") or 0) > 0,
            (
                f"write_bytes={summary.get('checkpoint_process_write_bytes')}, "
                f"logical_bytes={summary.get('checkpoint_snapshot_logical_bytes')}"
            ),
        )
        check(
            "checkpoint_kernel_peak_available",
            _number(summary, "checkpoint_kernel_operation_peak_bytes") is not None,
            (
                "kernel_operation_peak_bytes="
                f"{summary.get('checkpoint_kernel_operation_peak_bytes')!r}"
            ),
        )
        admission = summary.get("atomic_memory_admission") or {}
        checkpoint_leases = [
            item for item in admission.get("completed_leases", [])
            if item.get("request_class") == "checkpoint"
        ]
        check(
            "checkpoint_reservation_covered_peak",
            bool(checkpoint_leases) and all(
                int(item.get("parent_underprediction_bytes") or 0) == 0
                and int(item.get("tool_underprediction_bytes") or 0) == 0
                for item in checkpoint_leases
            ),
            f"checkpoint_leases={len(checkpoint_leases)}",
        )
        restore_leases = [
            item for item in admission.get("completed_leases", [])
            if item.get("request_class") == "restore"
        ]
        check(
            "restore_reservation_covered_peak",
            bool(restore_leases) and all(
                int(item.get("parent_underprediction_bytes") or 0) == 0
                and int(item.get("tool_underprediction_bytes") or 0) == 0
                for item in restore_leases
            ),
            f"restore_leases={len(restore_leases)}",
        )
        estimator = summary.get("wait_estimator")
        check(
            "oracle_wait_labeled",
            estimator != "oracle" or summary.get("wait_estimator_role") == "upper_bound",
            f"estimator={estimator!r}, role={summary.get('wait_estimator_role')!r}",
        )

    if reclamation in {"balloon", "hybrid"}:
        events = int(summary.get("tool_balloon_events") or 0)
        reached = int(summary.get("tool_balloon_target_reached_events") or 0)
        check(
            "balloon_targets_reached",
            events > 0 and reached == events,
            f"reached={reached}, events={events}",
        )

    if formal:
        check(
            "prediction_coverage",
            summary.get("admission_policy") not in {"p90", "oracle"}
            or int(summary.get("prediction_observations") or 0) > 0,
            f"observations={summary.get('prediction_observations')}",
        )
        check(
            "no_memory_max_events",
            int((summary.get("vm_pool_memory_events") or {}).get("max", 0)) == 0,
            f"events={summary.get('vm_pool_memory_events')}",
        )

    passed = all(item["passed"] for item in checks)
    return {
        "schema_version": 1,
        "mode": "formal" if formal else "pilot",
        "passed": passed,
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", type=Path)
    parser.add_argument("--mode", choices=("pilot", "formal"), default="formal")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_summary(
        json.loads(args.summary.read_text(encoding="utf-8")),
        formal=args.mode == "formal",
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
