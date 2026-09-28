from __future__ import annotations

import csv
import json

from clawbox.experiments.reporting import render_run_report, write_run_report_artifacts


def test_report_includes_policy_memory_prediction_and_warm_evidence(tmp_path) -> None:
    run = tmp_path / "run-1"
    arms = run / "arms"
    arms.mkdir(parents=True)
    events = run / "events.jsonl"
    events.write_text("\n".join([
        json.dumps({
            "event": "memory_sample", "warm_committed_bytes": 2 * 1024 ** 3,
            "warm_allocated_bytes": 3 * 1024 ** 3,
        }),
        json.dumps({
            "event": "sandbox_paused",
            "lifecycle_timing": {"snapshot_metrics": {
                "transferred_bytes": 1024 ** 3,
            }},
        }),
    ]) + "\n", encoding="utf-8")
    (arms / "arm.json").write_text(json.dumps({
        "experiment_id": "experiment-1", "status": "succeeded",
        "arm": {"concurrency": 16, "policy": {"name": "tool-p50-wait-reactive"}},
        "correctness": {
            "completed_sessions": 16, "native_tool_exact_id_join_rate": 1.0,
            "native_tool_telemetry_loss_total": 0, "cleanup_verified": True,
        },
        "performance": {
            "agents_per_minute": 2.0, "jct_p50_seconds": 30,
            "jct_p95_seconds": 35, "blocked_admission_seconds": 4,
            "pause_count": 2, "resume_count": 2,
            "pause_service_seconds": 1, "resume_service_seconds": .5,
            "prediction_coverage_fraction": .9,
            "prediction_source_distribution": {"frozen_clawbox_p50": 20},
            "prediction_error_p90_mib": 12,
            "local_high_overshoot_seconds": 1.5,
        },
        "memory": {
            "peak_used_delta_bytes": 20 * 1024 ** 3,
            "peak_local_tier_bytes": 20 * 1024 ** 3,
            "host_peak_used_delta_bytes": 22 * 1024 ** 3,
            "host_oom_kill_events": 0, "pool_budget_exceeded": False,
        },
        "artifacts": {"events": str(events)},
    }), encoding="utf-8")

    report = render_run_report(run)

    assert "tool-p50-wait-reactive" in report
    assert "20.00" in report
    assert "WARM transferred GiB" in report
    assert "| 1.00 | 2.00 |" in report
    assert "frozen_clawbox_p50:20" in report


def test_report_writes_plot_ready_memory_trace_and_repetition_summary(tmp_path) -> None:
    run = tmp_path / "run-1"
    arms = run / "arms"
    arms.mkdir(parents=True)
    for repetition, local_gib in ((1, 20), (2, 22)):
        arm_id = f"arm-{repetition}"
        events = run / "attempts" / "attempt-1" / arm_id / "events" / f"{arm_id}.jsonl"
        events.parent.mkdir(parents=True)
        events.write_text("\n".join([
            json.dumps({
                "event": "memory_sample", "monotonic_time_ns": "1000000000",
                "local_used_bytes": local_gib * 1024 ** 3,
                "shared_live_used_bytes": 1024 ** 3,
                "combined_live_used_bytes": (local_gib + 1) * 1024 ** 3,
                "warm_allocated_bytes": 2 * 1024 ** 3,
                "warm_committed_bytes": 1024 ** 3,
                "host_used_delta_bytes": (local_gib + 3) * 1024 ** 3,
                "host_used_bytes": 100 * 1024 ** 3,
                "host_available_bytes": 1900 * 1024 ** 3,
            }),
            json.dumps({
                "event": "memory_sample", "monotonic_time_ns": "1500000000",
                "local_used_bytes": (local_gib + 1) * 1024 ** 3,
            }),
        ]) + "\n", encoding="utf-8")
        (arms / f"{arm_id}.json").write_text(json.dumps({
            "experiment_id": "experiment-1", "status": "succeeded",
            "arm": {"arm_id": arm_id, "concurrency": 16, "repetition": repetition,
                    "policy": {"name": "tool-p50-wait-reactive"}},
            "correctness": {"completed_sessions": 16, "cleanup_verified": True},
            "performance": {"agents_per_minute": repetition,
                            "jct_p95_seconds": 30 + repetition,
                            "blocked_admission_seconds": repetition,
                            "local_high_overshoot_seconds": repetition},
            "memory": {"peak_local_tier_bytes": local_gib * 1024 ** 3,
                       "host_peak_used_delta_bytes": (local_gib + 3) * 1024 ** 3,
                       "host_oom_kill_events": 0},
            "artifacts": {"events": "/stale/remote/path.jsonl"},
        }), encoding="utf-8")

    report_path, csv_path = write_run_report_artifacts(run)

    assert "Cross-repetition summary" in report_path.read_text(encoding="utf-8")
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 4
    assert rows[0]["elapsed_seconds"] == "0.000000"
    assert rows[1]["elapsed_seconds"] == "0.500000"
    assert rows[0]["shared_total_gib"] == "3.000000"
    assert rows[0]["host_used_delta_gib"] == "23.000000"
