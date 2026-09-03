from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from clawbox.experiments import ExperimentSpec, expand_matrix, load_experiment, spec_digest
from clawbox.experiments.worker import ExperimentWorker


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


def test_v2_rejects_backend_transport_and_invalid_policy() -> None:
    raw = raw_spec()
    raw["sandbox"]["backend"] = "kubernetes"
    with pytest.raises(ValidationError, match="backend"):
        ExperimentSpec.model_validate(raw)
    raw = raw_spec()
    raw["policies"][0]["eviction"] = "eager"
    with pytest.raises(ValidationError, match="resident requires"):
        ExperimentSpec.model_validate(raw)


def test_openclaw_requires_api_inference_and_secret_credentials() -> None:
    raw = raw_spec()
    raw["agent"] = {"driver": "openclaw"}
    with pytest.raises(ValidationError, match="requires inference.backend=api"):
        ExperimentSpec.model_validate(raw)
    raw["inference"] = {"backend": "api", "configuration": {"api_key": "do-not-store"}}
    with pytest.raises(ValidationError, match="Kubernetes Secret"):
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
