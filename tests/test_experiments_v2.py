from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from clawbox.experiments import ExperimentSpec, expand_matrix, load_experiment, spec_digest
from clawbox.experiments.audit import audit_experiments
from clawbox.experiments.baselines import BASELINES, resolve_baseline
from clawbox.replay.trace import load_trace
from clawbox.experiments.worker import (
    EventWriter, ExperimentWorker, _runtime_network_deny_out, build_time_spans,
)


def test_runtime_network_policy_honors_explicit_internet_access() -> None:
    allowlist = ["192.0.2.10/32"]

    assert _runtime_network_deny_out(True, allowlist) is None
    assert _runtime_network_deny_out(False, allowlist) == ["0.0.0.0/0"]
    assert _runtime_network_deny_out(False, []) is None


def raw_spec() -> dict:
    return {
        "schema_version": 2,
        "experiment_id": "unit",
        "workload": {
            "source": "recorded_trace", "input": "trace.jsonl", "repetitions": 2,
            "cases": [{"case_id": "case-a", "source": "recorded_trace",
                       "source_reference": "trace.jsonl", "replay_trace_reference": "trace.jsonl"}],
        },
        "agent": {"driver": "replay_engine"},
        "inference": {"backend": "replay"},
        "runtime": {"template_alias": "runtime-arm64", "vcpu": 1, "memory_mib": 2048},
        "sandbox": {"template_alias": "task-arm64", "vcpu": 2, "memory_mib": 4096},
        "execution": {"concurrency_levels": [1, 4], "random_seed": 17},
        "resources": {
            "target_node": "node-a", "pool_memory_budget_mib": 100000,
            "emergency_free_memory_mib": 10000, "p90_predictions": "p90.json",
        },
        "policies": [
            {"name": "resident", "admission": "lifetime_full", "reclamation": "resident",
             "eviction": "none", "restore": "none"},
            {"name": "proposed", "admission": "tool_p90", "reclamation": "snapshot_pause",
             "eviction": "wait_aware_pressure", "restore": "proactive",
             "prefetch_lead_seconds": 0.5},
        ],
    }


def test_v2_matrix_is_complete_stable_and_randomized() -> None:
    spec = ExperimentSpec.model_validate(raw_spec())
    first = expand_matrix(spec)
    second = expand_matrix(spec)
    assert len(first) == 2 * 2 * 2
    assert [arm.arm_id for arm in first] == [arm.arm_id for arm in second]
    assert all(arm.spec_digest == spec_digest(spec) for arm in first)
    assert {arm.policy.name for arm in first} == {"resident", "proposed"}


def test_build_time_spans_reports_agent_and_sandbox_durations() -> None:
    spans = build_time_spans({
        "session_started": 1.0,
        "sandbox_create_start": 2.0,
        "tool_create_start": 2.0,
        "tool_ready": 3.5,
        "runtime_create_start": 3.0,
        "runtime_ready": 4.0,
        "sandbox_ready": 4.0,
        "agent_execution_start": 4.5,
        "final_agent_completion": 9.0,
        "validation_start": 9.0,
        "validation_end": 9.5,
        "output_hash_start": 9.5,
        "output_hash_end": 10.0,
        "sandbox_cleanup_start": 10.0,
        "sandbox_cleanup_end": 11.0,
        "session_finished": 11.0,
        "sandbox_create_gate_wait_start": 1.0,
        "sandbox_create_gate_acquired": 2.0,
    })
    by_name = {item["name"]: item for item in spans}
    assert by_name["agent"]["duration_seconds"] == 4.5
    assert by_name["sandbox.create"]["duration_seconds"] == 2.0
    assert by_name["sandbox.create.queue"]["duration_seconds"] == 1.0
    assert by_name["sandbox.cleanup"]["duration_seconds"] == 1.0


def test_worker_provenance_separates_runtime_and_tool_artifacts() -> None:
    raw = raw_spec()
    raw["runtime"].update({
        "template_id": "runtime-id", "template_alias": None,
        "source_image_reference": "registry/runtime",
        "image_digest": "sha256:" + "a" * 64,
    })
    raw["sandbox"].update({
        "template_id": "tool-id", "template_alias": None,
        "source_image_reference": "registry/tool",
        "image_digest": "sha256:" + "b" * 64,
    })
    worker = object.__new__(ExperimentWorker)
    worker.spec = ExperimentSpec.model_validate(raw)
    provenance = worker._provenance()
    assert provenance["runtime_template_reference"] == "runtime-id"
    assert provenance["runtime_template_image_digest"] == "sha256:" + "a" * 64
    assert provenance["tool_template_reference"] == "tool-id"
    assert provenance["tool_template_image_digest"] == "sha256:" + "b" * 64
    assert provenance["template_reference"] == "tool-id"  # legacy key


def test_event_writer_assigns_joinable_wall_and_monotonic_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    writer = EventWriter(path)
    writer.write({"event": "agent_started", "session_id": "session-a"})
    writer.write({"event": "agent_finished", "session_id": "session-a"})
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["sequence_no"] for row in rows] == [0, 1]
    assert all(row["schema_version"] == 1 for row in rows)
    assert all(row["session_id"] == "session-a" for row in rows)
    assert all(int(row["wall_time_ns"]) > 0 for row in rows)
    assert rows[0]["monotonic_time_ns"] <= rows[1]["monotonic_time_ns"]


def test_formal_openclaw_replay_c40_artifact_is_loadable() -> None:
    spec = load_experiment(Path("examples/experiments/openclaw-cube-replay-c40.yaml"))
    actions = load_trace(Path("examples/traces/openclaw-cube-replay.jsonl"))
    assert spec.agent.driver.value == "openclaw"
    assert spec.execution.concurrency_levels == (40,)
    assert [policy.name for policy in spec.policies] == ["resident", "eager-reactive"]
    assert actions[0].output["tool_calls"][0]["function"]["name"] == "exec"
    assert json.loads(actions[0].output["tool_calls"][0]["function"]["arguments"])[
        "command"
    ].startswith("printf openclaw-cube-replay-ok")


def test_checked_in_baseline_matrices_are_schema_v2_and_plan_c40() -> None:
    paths = list(Path("examples/experiments").glob("*.yaml"))
    audits = audit_experiments(paths)
    assert len(audits) == 8
    by_id = {str(item["experiment_id"]): item for item in audits}
    for experiment_id in ("decision", "full-system", "reclamation", "spatial"):
        assert by_id[experiment_id]["concurrency_levels"] == [20, 40, 60]
    assert by_id["openclaw-cube-replay-c40"]["concurrency_levels"] == [40]
    assert all(item["tool_template"] != "sandbox-code" for item in audits)
    openclaw = by_id["openclaw-cube-replay-c40"]["artifact_provenance"]
    assert openclaw["runtime"]["template_id"] == "tpl-39efe4ad90384a1fbea3caff"
    assert openclaw["tool"]["template_id"] == "tpl-b5cb6f5ee26a41448000b9c2"
    assert openclaw["runtime"]["image_digest"].startswith("sha256:")
    assert openclaw["tool"]["image_digest"].startswith("sha256:")


def test_openclaw_matrix_requires_immutable_runtime_and_tool_provenance(
    tmp_path: Path,
) -> None:
    raw = raw_spec()
    raw["agent"] = {"driver": "openclaw"}
    raw["inference"] = {
        "backend": "replay", "configuration": {"model": "recorded-model"},
    }
    path = tmp_path / "openclaw.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable template_id"):
        audit_experiments([path])

    raw["runtime"] = {
        "template_id": "runtime-id", "source_image_reference": "registry/runtime",
        "image_digest": "sha256:" + "a" * 64,
    }
    raw["sandbox"] = {
        "template_id": "tool-id", "source_image_reference": "registry/tool",
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="missing image_digest"):
        audit_experiments([path])


def test_baseline_catalog_materializes_only_current_policy_tuples() -> None:
    assert BASELINES
    for name, baseline in BASELINES.items():
        policy = baseline.as_policy()
        assert policy.name == name
        assert policy.admission == baseline.admission_policy
        assert policy.reclamation == baseline.reclamation_policy
    assert resolve_baseline("p90-elastic-pressure-checkpoint").as_policy().eviction.value == (
        "wait_aware_pressure"
    )


def test_openclaw_cube_example_uses_native_tool_names() -> None:
    spec = load_experiment(Path("examples/experiments/openclaw-cube.yaml"))
    assert "cube_shell" not in spec.workload.cases[0].prompt
    assert "sandboxed exec" in spec.workload.cases[0].prompt


def test_worker_bounds_pair_creation_for_high_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAWBOX_SANDBOX_CREATE_CONCURRENCY", "3")
    assert ExperimentWorker._sandbox_create_limit(40) == 3
    assert ExperimentWorker._sandbox_create_limit(2) == 2
    monkeypatch.setenv("CLAWBOX_SANDBOX_CREATE_CONCURRENCY", "0")
    with pytest.raises(ValueError, match="positive integer"):
        ExperimentWorker._sandbox_create_limit(40)


def test_v2_rejects_backend_transport_and_invalid_policy() -> None:
    raw = raw_spec()
    raw["sandbox"]["backend"] = "kubernetes"
    with pytest.raises(ValidationError, match="backend"):
        ExperimentSpec.model_validate(raw)
    raw = raw_spec()
    raw["policies"][0]["eviction"] = "eager"
    with pytest.raises(ValidationError, match="resident requires"):
        ExperimentSpec.model_validate(raw)


def test_openclaw_allows_managed_api_or_replay_and_keeps_secret_credentials_out_of_spec() -> None:
    raw = raw_spec()
    raw["agent"] = {"driver": "openclaw"}
    assert ExperimentSpec.model_validate(raw).inference.backend.value == "replay"
    raw["inference"] = {"backend": "api", "configuration": {"api_key": "do-not-store"}}
    with pytest.raises(ValidationError, match="environment variable"):
        ExperimentSpec.model_validate(raw)


def test_yaml_loader_and_v1_error(tmp_path: Path) -> None:
    path = tmp_path / "experiment.yaml"
    import yaml
    path.write_text(yaml.safe_dump(raw_spec()), encoding="utf-8")
    assert load_experiment(path).schema_version == 2
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="migrate schema v1"):
        load_experiment(path)


def test_worker_only_reuses_successful_complete_arm(tmp_path: Path) -> None:
    result = tmp_path / "arm.json"
    marker = tmp_path / "arm.complete"
    marker.write_text("digest\n", encoding="ascii")
    result.write_text(json.dumps({"arm": {"spec_digest": "digest"}, "status": "failed"}))
    assert not ExperimentWorker._completed(result, marker, "digest")
    result.write_text(json.dumps({"arm": {"spec_digest": "digest"}, "status": "succeeded"}))
    assert ExperimentWorker._completed(result, marker, "digest")
