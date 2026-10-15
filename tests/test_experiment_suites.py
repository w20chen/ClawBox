from pathlib import Path
import json

import pytest
import yaml

from clawbox.experiments.configure import configure_experiment, dump_experiment, experiment_overview
from clawbox.experiments.inputs import validate_inputs
from clawbox.experiments.spec import ExperimentSpec, expand_matrix


def admission(tmp_path, *, ablations=False):
    artifact = tmp_path / "training.json"
    artifact.write_text(json.dumps({
        "schema": "clawbox_prediction_v2", "repository": "owner/repo",
        "training_validated": True,
        "static_tool_memory_calibration": {
            "metric": "host_cgroup_memory_current_increment", "recommended_mib": 347,
        },
    }))
    return configure_experiment(
        Path("examples/experiments/getting-started.yaml"),
        suite="admission", suite_ablations=ablations,
        repository="owner/repo", prediction_artifact=str(artifact),
    )


def numa(*, ablations=False):
    raw = yaml.safe_load(Path("examples/experiments/agent-rl-heterogeneous-ablation-kunpeng.yaml").read_text())
    raw.update(suite="numa-snapshot", suite_ablations=ablations)
    raw.pop("policies", None)
    # The suite owns these settings, so a concise source need not specify them.
    raw["execution"].pop("placement_policy", None)
    raw["resources"].pop("memory_scheduling", None)
    return ExperimentSpec.model_validate(raw)


@pytest.mark.parametrize("ablations,count", [(False, 2), (True, 4)])
def test_admission_suite_defaults_to_no_admission(tmp_path, ablations, count):
    spec = admission(tmp_path, ablations=ablations)
    assert len(spec.policies) == count
    assert spec.resources.static_tool_memory_mib == (347 if ablations else None)
    assert spec.resources.prediction_mode == "runtime"
    assert all(a.resources.lifecycle_admission == "os-managed" for a in expand_matrix(spec))
    assert all(a.resources.proactive_cache_reclaim is False for a in expand_matrix(spec))
    assert all(a.execution.placement_policy == "os_default" for a in expand_matrix(spec))
    assert [p.admission.value for p in spec.policies[:2]] == ["off", "predicted"]
    assert [p.runtime_control.value for p in spec.policies[:2]] == ["off", "off"]
    assert spec.resources.memory_scheduling == "local_only"
    assert {p.reclamation.value for p in spec.policies} == {"resident"}


@pytest.mark.parametrize("ablations,count", [(False, 2), (True, 5)])
def test_numa_suite_matches_strong_baseline_resources(ablations, count):
    spec = numa(ablations=ablations)
    assert len(spec.policies) == count
    assert spec.execution.placement_policy == "load_aware"
    assert spec.resources.memory_scheduling == "joint"
    assert spec.resources.prediction_artifact is None
    arms = expand_matrix(spec)
    assert all(a.resources == spec.resources for a in arms if a.policy.deployment == "managed")
    default = next(a for a in arms if a.policy.deployment == "os-default")
    assert default.execution.placement_policy == "os_default"
    assert default.resources.lifecycle_admission == "os-managed"
    assert default.resources.local_memory_capacity_mib == spec.resources.local_memory_capacity_mib + spec.resources.shared_memory_borrow_limit_mib
    assert default.resources.shared_memory_borrow_limit_mib is None
    assert default.resources.compute_nodes == ()
    assert default.policy.runtime_control.value == "off"
    assert default.policy.reclamation.value == "resident"
    assert {p.admission.value for p in spec.policies} == {"off"}
    assert {p.eviction.value for p in spec.policies} == {"none", "wait_aware_pressure"}
    assert spec.policies[0].snapshot_aware_residency is False
    assert spec.policies[-1].snapshot_aware_residency is True


def test_suite_recipes_and_full_frozen_specs_roundtrip(tmp_path):
    for spec in (admission(tmp_path), numa()):
        compact = yaml.safe_load(dump_experiment(spec, compact=True))
        assert "policies" in yaml.safe_load(dump_experiment(spec))
        assert "policies" not in compact
        assert ExperimentSpec.model_validate(compact) == spec
        assert ExperimentSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_configure_can_reuse_compact_suite(tmp_path):
    original = admission(tmp_path)
    path = tmp_path / "base.yaml"
    path.write_text(dump_experiment(original, compact=True))
    updated = configure_experiment(path, concurrency="2,4")
    assert updated.execution.concurrency_levels == (2, 4)
    assert updated.policies == original.policies


@pytest.mark.parametrize("section,key,value", [
    ("execution", "placement_policy", "round_robin"),
    ("resources", "lifecycle_admission", "os-managed"),
    ("resources", "template_base_sharing", "disabled"),
])
def test_frozen_suite_rejects_confounding_overrides(section, key, value):
    raw = numa().model_dump(mode="json")
    raw[section][key] = value
    with pytest.raises(ValueError, match="suite requires"):
        ExperimentSpec.model_validate(raw)


def test_suite_rejects_hand_modified_policy():
    raw = numa().model_dump(mode="json")
    raw["policies"][0]["runtime_control"] = "agentcgroup_cgroup"
    with pytest.raises(ValueError, match="suite policies are generated"):
        ExperimentSpec.model_validate(raw)


def test_suite_can_tune_fixed_comparator(tmp_path):
    original = admission(tmp_path, ablations=True)
    path = tmp_path / "base.yaml"
    path.write_text(dump_experiment(original, compact=True))
    spec = configure_experiment(path, static_tool_memory_mib=123)
    assert spec.resources.static_tool_memory_mib == 123
    assert spec.policies[0].admission.value == "off"


def test_runtime_control_switch_keeps_deployment_baseline_off(tmp_path):
    spec = admission(tmp_path)
    path = tmp_path / "base.yaml"
    path.write_text(dump_experiment(spec, compact=True))
    enabled = configure_experiment(path, runtime_control="agentcgroup_cgroup")
    assert enabled.policies[0].runtime_control.value == "off"
    assert enabled.policies[1].runtime_control.value == "agentcgroup_cgroup"
    disabled = configure_experiment(path, runtime_control="off")
    assert all(p.runtime_control.value == "off" for p in disabled.policies)


def test_zero_snapshot_value_is_visible():
    assert "zero" in experiment_overview(numa())["warnings"][0]


def test_heterogeneous_overview_counts_assigned_cases():
    raw = numa().model_dump(mode="json")
    raw["workload"]["cases"][1]["sandbox"].update(memory_mib=2048, vcpu=1)
    overview = experiment_overview(ExperimentSpec.model_validate(raw))
    row = overview["concurrency"][0]
    assert row["agents"] == 3
    assert row["offered_pair_memory_gib"] == 16
    assert row["offered_vcpu"] == 11
    assert overview["heterogeneous_tool_shapes"] is True
