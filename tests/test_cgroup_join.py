"""Tests for the independent cgroup v2/procfs artifact pipeline.

Covers the three-source join: ClawTune span + tool-bridge record + the
Tool-VM cgroup artifact (ClawTune ``cgroup_resource_v1`` format), which is the
data path that carries independently sourced guest resource accounting into the
observation dataset and KB.
"""

from __future__ import annotations

import json

from clawbox.tuning.dataset import build_joined_dataset, read_cgroup_artifacts
from clawbox.tuning.join import join_trace_and_bridge
from clawbox.tuning.schema import (
    BridgeRecord,
    CgroupResource,
    cgroup_artifact_to_resource,
)


def cgroup_artifact(execution_id: str, **overrides) -> dict:
    data = {
        "schema": "cgroup_resource_v1",
        "execution_id": execution_id,
        "tool_call_id": f"call-{execution_id}",
        "tool_name": "exec",
        "source": "cgroup-v2",
        "monitor_source": "cgroup-v2",
        "attribution_source": "exclusive-execution-cgroup",
        "ts_start": 1_700_000_000.0,
        "ts_end": 1_700_000_005.0,
        "duration_ms": 5000,
        "cpu_time_s": 4.2,
        "cpu_utilization_avg_cores": 1.4,
        "memory_rss_before_bytes": 1000,
        "memory_rss_after_bytes": 2000,
        "memory_rss_peak_bytes": 4096,
        "disk_read_bytes_delta": 100,
        "disk_write_bytes_delta": 200,
        "network_rx_bytes_delta": None,
        "network_tx_bytes_delta": None,
        "sampling_interval_ms": 100,
        "sampling_point_count": 50,
        "sampling_quality": "valid",
        "sampling_coverage_ms": 4900,
        "cpu_source": "cgroup-v2-cpu.stat",
        "memory_source": "cgroup-v2-memory.peak",
        "disk_source": "cgroup-v2-io.stat",
        "network_source": "unavailable",
        "fallback_used": False,
        "collector_errors": [],
        "independence": "independent resource accounting; not eBPF clause telemetry",
    }
    data.update(overrides)
    return data


def bridge_record(execution_id: str, exit_code: int = 0) -> BridgeRecord:
    return BridgeRecord(
        timestamp="2026-08-19T00:00:00Z",
        execution_id=execution_id,
        execution_source="runtime-envelope",
        duration_ms=5000,
        exit_code=exit_code,
    )


def span_end(execution_id: str) -> dict:
    wall_ns = int(1_700_000_000_000_000_000)
    return {
        "schema_version": 6,
        "record_type": "span_end",
        "kind": "tool",
        "name": "exec",
        "wall_time_ns": str(wall_ns),
        "duration_sec": "5.0",
        "duration_ns": "5000000000",
        "repo": "github.com/acme/foo",
        "status": {"code": "ok"},
        "output": {"exit_code": 0},
        "execution": {
            "execution_id": execution_id,
            "requested_command": "pytest -q",
            "command_digest": "abc",
        },
        "resources": {
            "coverage_ratio": 1.0,
            "monitor_start_wall_time_ns": str(wall_ns - 5_000_000_000),
            "action_duration_ns": "5000000000",
            "cpu_time_s": "4.2",
            "cpu_utilization_avg_cores": "1.4",
            "rss_peak_bytes": 4096,
        },
    }


def test_cgroup_artifact_parser_valid():
    resource = cgroup_artifact_to_resource(cgroup_artifact("exec-1"))
    assert resource is not None
    assert isinstance(resource, CgroupResource)
    assert resource.execution_id == "exec-1"
    assert resource.source == "cgroup-v2"
    assert resource.cpu_time_s == 4.2
    assert resource.memory_rss_peak_bytes == 4096
    assert resource.network_tx_bytes_delta is None
    assert resource.network_source == "unavailable"
    assert resource.fallback_used is False


def test_bridge_record_accepts_current_clawtune_telemetry_metadata() -> None:
    from clawbox.tuning.schema import BridgeRecord

    value = BridgeRecord.model_validate({
        "timestamp": "2026-08-30T00:00:00Z", "execution_id": "exec-1",
        "duration_ms": 10, "exit_code": 0,
        "telemetry_state": "complete", "telemetry_artifact": "/tmp/a.json",
        "telemetry_eligible_for_kb": True, "telemetry_quality": "ok",
        "telemetry_collection_validity": "valid", "telemetry_cleanup": "ok",
        "telemetry_loss_total": 0,
    })
    assert value.telemetry_eligible_for_kb is True
    assert value.telemetry_loss_total == 0


def test_cgroup_artifact_parser_rejects_invalid():
    assert cgroup_artifact_to_resource({}) is None
    assert cgroup_artifact_to_resource({"schema": "other"}) is None
    assert cgroup_artifact_to_resource(cgroup_artifact("")) is None
    assert cgroup_artifact_to_resource(cgroup_artifact("exec-1", cpu_time_s="not-a-number")) is None


def test_read_cgroup_artifacts_from_trace_dir(tmp_path):
    trace_dir = tmp_path / "traces"
    (trace_dir / "tool-resource").mkdir(parents=True)
    (trace_dir / "tool-resource" / "cgroup-resource-exec-1.json").write_text(
        json.dumps(cgroup_artifact("exec-1")), encoding="utf-8"
    )
    (trace_dir / "cgroup-resource-exec-2.json").write_text(
        json.dumps(cgroup_artifact("exec-2")), encoding="utf-8"
    )
    (trace_dir / "cgroup-resource-bad.json").write_text("{not json", encoding="utf-8")
    artifacts = read_cgroup_artifacts(trace_dir)
    assert set(artifacts) == {"exec-1", "exec-2"}


def test_three_source_join_attaches_cgroup():
    artifacts = {"exec-1": CgroupResource.model_validate(cgroup_artifact("exec-1"))}
    result = join_trace_and_bridge(
        [span_end("exec-1")],
        [bridge_record("exec-1")],
        artifacts,
    )
    assert result.join_rate == 1.0
    assert len(result.joined) == 1
    merged = result.joined[0]
    assert merged.cgroup is not None
    assert merged.cgroup.cpu_time_s == 4.2
    assert merged.cgroup.memory_rss_peak_bytes == 4096
    assert merged.cgroup.source == "cgroup-v2"
    assert merged.collection_quality == "valid"
    assert merged.exit_code == 0
    assert merged.trusted is True


def test_unmatched_cgroup_artifacts_reported():
    artifacts = {"exec-1": CgroupResource.model_validate(cgroup_artifact("exec-1"))}
    result = join_trace_and_bridge([], [], artifacts)
    assert len(result.unmatched_cgroup) == 1
    assert result.unmatched_cgroup[0].execution_id == "exec-1"


def test_build_joined_dataset_carries_cgroup(tmp_path):
    trace_dir = tmp_path / "traces"
    (trace_dir / "tool-resource").mkdir(parents=True)
    (trace_dir / "run-a.jsonl").write_text(
        json.dumps(span_end("exec-1")) + "\n", encoding="utf-8"
    )
    (trace_dir / "tool-resource" / "cgroup-resource-exec-1.json").write_text(
        json.dumps(cgroup_artifact("exec-1")), encoding="utf-8"
    )
    bridge = tmp_path / "tool-bridge.jsonl"
    bridge.write_text(
        json.dumps(bridge_record("exec-1").model_dump(mode="json")), encoding="utf-8"
    )
    joined, trusted = build_joined_dataset(trace_dir, bridge)
    assert joined.join_rate == 1.0
    assert len(trusted) == 1
    assert trusted[0].cgroup is not None
    assert trusted[0].cgroup.cpu_time_s == 4.2


def test_degraded_process_tree_artifact_cannot_enter_trusted_kb(tmp_path):
    trace_dir = tmp_path / "traces"
    (trace_dir / "tool-resource").mkdir(parents=True)
    (trace_dir / "run-a.jsonl").write_text(
        json.dumps(span_end("exec-1")) + "\n", encoding="utf-8"
    )
    fallback = cgroup_artifact(
        "exec-1",
        source="process-tree",
        monitor_source="psutil-process-tree",
        sampling_quality="degraded",
        cpu_source="procfs-process-tree",
        memory_source="procfs-process-tree",
        disk_source="procfs-process-tree",
        fallback_used=True,
    )
    (trace_dir / "tool-resource" / "cgroup-resource-exec-1.json").write_text(
        json.dumps(fallback), encoding="utf-8"
    )
    bridge = tmp_path / "tool-bridge.jsonl"
    bridge.write_text(
        json.dumps(bridge_record("exec-1").model_dump(mode="json")), encoding="utf-8"
    )

    joined, trusted = build_joined_dataset(trace_dir, bridge)

    assert joined.joined[0].collection_quality == "degraded"
    assert joined.joined[0].trusted is False
    assert trusted == []
