from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from clawbox.experiments import ExperimentSpec, expand_matrix, load_experiment, spec_digest
from clawbox.experiments.worker import ExperimentWorker, build_time_spans


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
    })
    by_name = {item["name"]: item for item in spans}
    assert by_name["agent"]["duration_seconds"] == 4.5
    assert by_name["sandbox.create"]["duration_seconds"] == 2.0
    assert by_name["sandbox.cleanup"]["duration_seconds"] == 1.0


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
