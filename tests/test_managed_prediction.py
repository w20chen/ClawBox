from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import pytest

from clawbox.experiments.prediction import CommandPredictionProvider, PredictionUnavailable, clawtune_extra_peak


def test_clawtune_extra_peak_reserves_only_peak_above_baseline() -> None:
    prediction = {
        "schema_version": "call_load.v2", "scope": "tool_call",
        "memory_measurement": "guest_memtotal_minus_memavailable",
        "targets": {"memory_extra_peak_bytes": {
            "status": "available", "unit": "bytes",
            "metric_definition": "environment_memory_peak_minus_baseline",
            "p90": 10 * 1024 * 1024 + 1, "backend": "lattice",
            "method": "direct", "sample_count": 3,
        }},
    }
    result = clawtune_extra_peak(prediction)
    assert result["predicted_incremental_memory_mib"] > 10
    assert result["admission_prediction_target"] == "environment_memory_peak_minus_baseline"
    prediction["targets"]["memory_extra_peak_bytes"]["status"] = "unavailable"
    with pytest.raises(PredictionUnavailable, match="unavailable"):
        clawtune_extra_peak(prediction)


def test_clawtune_total_peak_cannot_substitute_for_extra_peak() -> None:
    with pytest.raises(PredictionUnavailable, match="unavailable"):
        clawtune_extra_peak({"schema_version": "call_load.v2", "scope": "tool_call",
                             "memory_measurement": "guest_memtotal_minus_memavailable",
                             "targets": {"memory_total_peak_bytes": {"p90": 1024}}})


def test_clawtune_memory_measurement_must_match_cube_guest() -> None:
    with pytest.raises(PredictionUnavailable, match="Cube guest"):
        clawtune_extra_peak({
            "schema_version": "call_load.v2", "scope": "tool_call",
            "memory_measurement": "cgroup_v2_environment_union_v1", "targets": {},
        })


def test_toolkb_prediction_cannot_enter_lattice_admission():
    with pytest.raises(PredictionUnavailable, match="LatticeKB"):
        clawtune_extra_peak({
            "schema_version": "call_load.v2", "scope": "tool_call",
            "memory_measurement": "guest_memtotal_minus_memavailable",
            "targets": {"memory_extra_peak_bytes": {"backend": "runtime", "p90": 100}},
        })


def _trainer_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "train-p90-from-runs.py"
    spec = importlib.util.spec_from_file_location("train_p90_from_runs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prediction_provider_uses_exact_runtime_command_metadata_and_freezes_hash(tmp_path: Path) -> None:
    command = "pytest -q tests/test_one.py"
    path = tmp_path / "p90.json"
    path.write_text(json.dumps({
        "generation": 7,
        "per_tool_memory": {"workloads": {"case": {"tool_invocations": [{
            "command": command,
            "command_sha256": __import__("hashlib").sha256(command.encode()).hexdigest(),
            "predicted_command_memory_p90_mib": 42.5,
            "predicted_host_execution_increment_mib": 51.0,
            "host_mapping_source": "recording-set-a",
            "host_mapping_ratio_p90": 1.2,
            "key_kind": "exact_command",
            "fallback_path": ["repo:exact_command"],
            "evidence_count": 12,
        }]}}},
    }), encoding="utf-8")
    provider = CommandPredictionProvider(path, repository="repo-a")
    metadata = provider.manifest[provider.manifest.keys().__iter__().__next__()]
    resolved = provider.resolve(command, metadata)
    assert resolved["canonical_prediction_key"] == "pytest -q tests/test_one.py"
    assert resolved["predicted_guest_memory_p90_mib"] == 42.5
    assert resolved["predicted_incremental_memory_mib"] == 51.0
    assert resolved["admission_prediction_target"] == "host_vm_rss_execution_increment"
    assert resolved["fallback_level"] == "exact_command"
    assert provider.provenance()["sha256"] == __import__("hashlib").sha256(path.read_bytes()).hexdigest()


def test_prediction_provider_fails_closed_for_missing_or_cross_command_metadata(tmp_path: Path) -> None:
    command = "true"
    path = tmp_path / "p90.json"
    path.write_text(json.dumps({"tool_invocations": [{
        "command": command, "predicted_command_memory_p90_mib": 1,
        "predicted_host_execution_increment_mib": 1,
    }]}), encoding="utf-8")
    provider = CommandPredictionProvider(path)
    with pytest.raises(PredictionUnavailable):
        provider.resolve("false", provider.manifest[next(iter(provider.manifest))])
    with pytest.raises(PredictionUnavailable):
        provider.resolve(command, None)


def test_prediction_provider_rejects_uncalibrated_guest_memory(tmp_path: Path) -> None:
    path = tmp_path / "uncalibrated.json"
    path.write_text(json.dumps({"tool_invocations": [{
        "command": "true", "predicted_command_memory_p90_mib": 1,
    }]}), encoding="utf-8")
    with pytest.raises(ValueError, match="calibrated positive host increment"):
        CommandPredictionProvider(path)


def test_prediction_provider_can_freeze_heldout_oracle_source(tmp_path: Path) -> None:
    path = tmp_path / "oracle.json"
    path.write_text(json.dumps({"tool_invocations": [{
        "command": "true",
        "predicted_command_memory_p90_mib": 2,
        "predicted_host_execution_increment_mib": 3,
    }]}), encoding="utf-8")
    provider = CommandPredictionProvider(
        path, prediction_source="runtime_tool_oracle_heldout"
    )
    metadata = provider.manifest[next(iter(provider.manifest))]

    assert metadata["prediction_source"] == "runtime_tool_oracle_heldout"
    assert provider.resolve("true", metadata)["predicted_incremental_memory_mib"] == 3


def test_host_calibration_excludes_backend_maintenance(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text(json.dumps({"performance": {"tool_execution_observations": [
        {"execution_scope": "backend-maintenance",
         "actual_measured_memory_mib": 1, "actual_host_execution_increment_mib": 1000},
        *[
            {"execution_scope": "agent-tool", "actual_measured_memory_mib": 2,
             "actual_host_execution_increment_mib": value}
            for value in (2, 4, 6, 8, 10)
        ],
    ]}}), encoding="utf-8")

    calibration = _trainer_module()._host_increment_calibration([path])

    assert calibration["pair_count"] == 5
    assert calibration["ratio_max"] == 5


def test_per_tool_plan_queries_guest_memory_model(monkeypatch, tmp_path):
    from tool_resource.runtime_kb import ClauseObservation, ToolCallQuery
    from tool_time.lattice_kb import LatticeTimeKB

    trainer = _trainer_module()
    (tmp_path / "trace").write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(trainer, "_trace_tool_calls", lambda path: [
        {"tool_name": "exec", "command": "python -m pytest", "model_step": 0, "call_index": 0},
    ])
    kb = LatticeTimeKB.fit([ClauseObservation(
        repo="repo", bin="python", argv=("python", "-m", "pytest"), ts_start=0, ts_end=2, latency_ms=2000,
        memory_baseline_bytes=1024**2, memory_total_peak_bytes=3 * 1024**2,
        memory_extra_peak_bytes=2 * 1024**2, memory_eligible=True,
        memory_measurement="guest_memtotal_minus_memavailable", memory_environment_id="cube:boot-1",
    )])
    plan = trainer._per_tool_memory_plan(
        kb, ToolCallQuery, repository="repo", traces={"case": tmp_path / "trace"},
        query_ts=3, idle_tool_vm_rss_mib=64, idle_safety_margin_fraction=0,
        command_headroom_fraction=0, size_classes_mib=[128],
        host_calibration={"source_sha256": "calibration", "ratio_p90": 1.5},
    )
    call = plan["workloads"]["case"]["tool_invocations"][0]
    assert call["predicted_command_memory_p90_mib"] == 2
    assert call["predicted_host_execution_increment_mib"] == 3


def test_training_cli_uses_guest_labels_without_process_rss(monkeypatch, tmp_path):
    import sys
    from tests.test_cgroup_join import span_end, bridge_record, cgroup_artifact
    from tests.test_native_tuning import make_manifest
    import base64

    run = tmp_path / "run"
    run.mkdir()
    spans, bridges = [], []
    for index in range(5):
        execution_id = f"exec-{index}"
        span = span_end(execution_id)
        span["execution"]["requested_command"] = "python -m pytest -q"
        span["resources"].pop("rss_peak_bytes")
        spans.append(json.dumps(span))
        bridges.append(bridge_record(execution_id).model_dump_json())
        resource = cgroup_artifact(
            execution_id, memory_rss_peak_bytes=None, memory_eligible=True,
            memory_baseline_bytes=1024**2, memory_total_peak_bytes=3 * 1024**2,
            memory_extra_peak_bytes=2 * 1024**2,
            memory_measurement="guest_memtotal_minus_memavailable", memory_environment_id="cube:boot-1",
        )
        native = make_manifest(execution_id)
        clause = json.loads(base64.b64decode(native.artifacts[0].content_b64))
        row = clause["calls"][0]["clauses"][0]
        row.update(ts_start=resource["ts_start"], ts_end=resource["ts_end"], latency_ms=5000)
        resource["memory_timeline"] = [[resource["ts_start"] - 0.001, 1024**2],
            *[[resource["ts_start"] + i / 10, 3 * 1024**2] for i in range(1, 51)]]
        (run / f"clause-telemetry-{execution_id}.json").write_text(json.dumps(clause), encoding="utf-8")
        (run / f"cgroup-resource-{execution_id}.json").write_text(json.dumps(resource), encoding="utf-8")
    (run / "trace.jsonl").write_text("\n".join(spans), encoding="utf-8")
    (run / "tool-bridge.jsonl").write_text("\n".join(bridges), encoding="utf-8")
    output = tmp_path / "p90.json"
    monkeypatch.setattr(sys, "argv", [
        "train-p90-from-runs.py", str(run), "--repository", "github.com/acme/foo",
        "--command", "python -m pytest -q",
        "--output", str(output),
        "--seed-output", str(tmp_path / "seed"),
    ])
    _trainer_module().main()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["training"]["completed_calls"] == 5
    assert payload["call_prediction"]["memory_measurement"] == "guest_memtotal_minus_memavailable"
    assert payload["call_prediction"]["targets"]["memory_extra_peak_bytes"]["p90"] == 2 * 1024**2
    from clawtune_kb import validate_seed
    validate_seed(tmp_path / "seed")
    assert json.loads((tmp_path / "seed" / "clause-lattice-time-kb.json").read_text()) == payload["lattice_snapshot"]
