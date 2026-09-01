from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest
from pydantic import ValidationError

from clawbox.experiments import (
    ExperimentSpec, ResultEnvelope, WorkloadSource, expand_matrix, failure_category_for, load_workload_cases,
    resolve_baseline, validate_workflow,
)
from clawbox.experiments.capabilities import CapabilityError, WorkflowClassification
from clawbox.experiments.results import FailureCategory, RunStatus
from clawbox.experiments.cli import main as experiments_main
from clawbox.benchmark.kubernetes import BenchmarkTask, KubernetesBenchmarkLauncher
from clawbox.replay.study import (
    _p90_prediction, _predictive_tool_memory_mib, expand_paper_policy_matrix,
    study_experiment_spec,
)


DIGEST = "registry.example/task@sha256:" + "a" * 64


def production_spec(**overrides):
    raw = {
        "workload": {"source": "swe_rebench", "input": "tasks.json"},
        "agent": {"driver": "openclaw"}, "inference": {"backends": ["api"]},
        "sandbox": {"backend": "kubernetes", "tool_transport": "ssh"},
        "scheduling": {"baselines": ["fixed-resident"]},
        "resources": {"profile": "small"},
    }
    raw.update(overrides)
    return ExperimentSpec.model_validate(raw)


def test_parses_all_workload_sources(tmp_path):
    swe = tmp_path / "tasks.json"
    swe.write_text('[{"instance_id":"swe-1","problem_statement":"fix it","repo":"org/repo"}]')
    synthetic = tmp_path / "synthetic.json"
    synthetic.write_text('{"cases":[{"case_id":"synthetic-1","prompt":"probe"}]}')
    trace = tmp_path / "recording.jsonl"
    trace.write_text('{}\n')
    assert load_workload_cases(production_spec(workload={"source": "swe_rebench", "input": str(swe)}).workload)[0].case_id == "swe-1"
    assert load_workload_cases(production_spec(workload={"source": "synthetic", "input": str(synthetic)}).workload)[0].source == WorkloadSource.SYNTHETIC
    assert load_workload_cases(production_spec(workload={"source": "recorded_trace", "input": str(trace)}).workload)[0].replay_trace_reference == str(trace)


def test_baseline_lookup_and_immutable_resolution():
    baseline = resolve_baseline("fixed-resident")
    assert baseline.admission_policy == "fixed_profile"
    with pytest.raises((AttributeError, ValidationError)):
        baseline.admission_policy = "p90_static"  # type: ignore[misc]
    workflow = expand_matrix(production_spec())[0]
    with pytest.raises(ValidationError):
        workflow.residency_policy = "llm_wait_checkpoint"  # type: ignore[misc]


def test_unknown_baseline_and_unsupported_combinations_are_rejected():
    with pytest.raises(ValueError, match="unknown baseline"):
        expand_matrix(production_spec(scheduling={"baselines": ["does-not-exist"]}))
    with pytest.raises(CapabilityError, match="checkpoint residency"):
        expand_matrix(production_spec(scheduling={"baselines": ["fixed-llm-wait-checkpoint"]}))


def test_matrix_crosses_only_inference_and_baseline_axes():
    spec = ExperimentSpec.model_validate({
        "workload": {"source": "recorded_trace", "input": "trace.jsonl"},
        "agent": {"driver": "openclaw"},
        "inference": {"backends": ["replay", "api"],
                      "configuration": {"replay": {"time_scale": 0.5},
                                        "api": {"model": "paper-model"}}},
        "sandbox": {"backend": "direct_firecracker", "tool_transport": "ssh",
                    "runtime_image": "runtime@sha256:one", "tool_image": "tool@sha256:two",
                    "materialization": {"runtime_rootfs": "runtime.ext4",
                                        "tool_rootfs": "tool.ext4", "prompt": "prompt.txt"}},
        "scheduling": {"baselines": ["fixed-explicit-resident", "fixed-llm-wait-checkpoint"]},
        "execution": {"concurrency": 7, "timeout_seconds": 901,
                      "command_timeout_seconds": 123},
        "resources": {"runtime_memory_mib": 2048, "tool_memory_mib": 4096,
                      "cpu_first": 4, "numa_node": 1},
        "validation": {"command": "check-state"}, "output": {"directory": "out"},
    })
    workflows = expand_matrix(spec)
    assert len(workflows) == 4
    assert {item.inference_backend for item in workflows} == {"replay", "api"}
    assert {item.residency_policy for item in workflows} == {"resident", "llm_wait_checkpoint"}
    assert all(item.sandbox_backend == "direct_firecracker" for item in workflows)
    assert all(item.resources.runtime_memory_mib == 2048 for item in workflows)
    assert all(item.execution.concurrency == 7 for item in workflows)
    assert all(item.sandbox.runtime_image == "runtime@sha256:one" for item in workflows)
    assert all(item.validation.command == "check-state" for item in workflows)
    assert all(item.output.directory == "out" for item in workflows)
    assert {item.inference_configuration.get("time_scale") for item in workflows
            if item.inference_backend == "replay"} == {0.5}
    assert all(validate_workflow(item).classification == WorkflowClassification.RESEARCH for item in workflows)


def test_capability_rules_accept_safe_p90_cells_but_reject_invalid_generation_use():
    elastic = expand_matrix(production_spec(
        scheduling={"baselines": ["p90-elastic"]},
    ))[0]
    assert validate_workflow(elastic).classification == WorkflowClassification.RESEARCH
    static = expand_matrix(production_spec(
        scheduling={"baselines": ["p90-static"]},
        resources={"profile": "small", "kb_generation": 7},
    ))[0]
    assert validate_workflow(static).classification == WorkflowClassification.RESEARCH
    with pytest.raises(CapabilityError, match="kb_generation is required"):
        expand_matrix(production_spec(scheduling={"baselines": ["p90-static"]}))
    with pytest.raises(CapabilityError, match="only valid"):
        expand_matrix(production_spec(
            scheduling={"baselines": ["p90-elastic"]},
            resources={"profile": "small", "kb_generation": 7},
        ))
    with pytest.raises(CapabilityError, match="resources.profile"):
        expand_matrix(production_spec(resources={}))

    historical = ExperimentSpec.model_validate({
        "workload": {"source": "recorded_trace", "input": "trace.jsonl"},
        "agent": {"driver": "replay_engine"}, "inference": {"backends": ["replay"]},
        "sandbox": {"backend": "local", "tool_transport": "local"},
        "scheduling": {"baselines": ["fixed-explicit-resident"]},
    })
    assert validate_workflow(expand_matrix(historical)[0]).classification == WorkflowClassification.HISTORICAL


def test_legacy_paper_study_translates_snapshot_and_generates_four_workflows():
    spec = study_experiment_spec({
        "output": "out", "source": {"trace": "trace.jsonl", "prompt": "prompt.txt",
            "runtime_rootfs": "runtime.ext4", "tool_rootfs": "tool.ext4"},
        "sessions": 2, "inference_backends": ["replay", "api"],
        "memory_policies": ["resident", "snapshot"],
    })
    workflows = expand_matrix(spec)
    assert len(workflows) == 4
    assert {item.residency_policy for item in workflows} == {"resident", "llm_wait_checkpoint"}


def test_replay_factorial_crosses_fixed_predictive_and_residency_without_hidden_inference_change():
    prediction_payload = {
        "tenant_id": "tenant-a", "repo_fingerprint": "org/repo", "generation": 4,
        "pair_digest": "a" * 64, "source_digest": "b" * 64,
        "artifact_count": 20, "clawtune_revision": "c" * 40,
        "prediction": {
            "latency_p90_sec": 12.0, "cpu_p90_cores": 0.7,
            "memory_p90_bytes": 1024**3, "evidence_count": 10,
            "scopes": {"memory": "public"}, "fallback_paths": {},
        },
    }
    raw = {
        "output": "out", "source": {"trace": "trace.jsonl", "prompt": "prompt.txt",
            "runtime_rootfs": "runtime.ext4", "tool_rootfs": "tool.ext4"},
        "sessions": 4, "inference_backends": ["replay"],
        "sizing_policies": ["fixed", "p90_static"],
        "memory_policies": ["resident", "snapshot"],
        "resources": {"runtime_memory_mib": 2048, "tool_memory_mib": 4096},
        "p90_static": {"prediction": prediction_payload, "min_tool_memory_mib": 2048},
    }
    workflows = expand_matrix(study_experiment_spec(raw))
    assert len(workflows) == 4
    assert {item.baseline for item in workflows} == {
        "fixed-explicit-resident", "fixed-llm-wait-checkpoint",
        "p90-static", "p90-static-llm-wait-checkpoint",
    }
    assert {item.inference_backend for item in workflows} == {"replay"}
    prediction = _p90_prediction(raw, None)
    assert prediction is not None and prediction.generation == 4
    assert _predictive_tool_memory_mib(raw, prediction) == 2048
    mismatched = {**raw, "source": {**raw["source"], "repository": "other/repo"}}
    with pytest.raises(ValueError, match="repository does not match"):
        _p90_prediction(mismatched, None)
    insufficient = {**raw, "p90_static": {
        **raw["p90_static"], "min_evidence": 11,
    }}
    with pytest.raises(ValueError, match="at least 11"):
        _p90_prediction(insufficient, None)


def test_paper_policy_matrices_keep_4_gib_capacity_and_axes_separate():
    base = {"resources": {"tool_memory_mib": 4096}}
    spatial = expand_paper_policy_matrix({**base, "paper_experiment": {
        "dimension": "spatial",
        "admission_policies": ["full_reservation", "static", "p90", "oracle"],
        "reclamation_policies": ["resident"],
        "restore_policies": ["reactive"],
    }})
    assert [arm["admission_policy"] for arm in spatial] == [
        "full_reservation", "static", "p90", "oracle",
    ]
    assert {arm["reclamation_policy"] for arm in spatial} == {"resident"}

    temporal = expand_paper_policy_matrix({**base, "paper_experiment": {
        "dimension": "temporal", "admission_policies": ["p90"],
        "reclamation_policies": ["resident", "balloon", "checkpoint", "hybrid"],
        "decision_policies": ["fixed_delay"],
    }})
    assert len(temporal) == 4
    assert {arm["admission_policy"] for arm in temporal} == {"p90"}

    predicted_temporal = expand_paper_policy_matrix({**base, "paper_experiment": {
        "dimension": "temporal", "admission_policies": ["p90"],
        "reclamation_policies": ["checkpoint", "hybrid"],
        "decision_policies": ["wait_aware_pressure"],
    }})
    assert {arm["decision_policy"] for arm in predicted_temporal} == {
        "wait_aware_pressure",
    }

    mechanism = expand_paper_policy_matrix({**base, "paper_experiment": {
        "dimension": "mechanism", "admission_policies": ["p90"],
        "reclamation_policies": ["resident", "balloon", "checkpoint"],
        "decision_policies": ["fixed_delay"],
    }})
    assert len(mechanism) == 3
    assert {arm["reclamation_policy"] for arm in mechanism} == {
        "resident", "balloon", "checkpoint",
    }
    assert next(
        arm for arm in mechanism if arm["reclamation_policy"] == "checkpoint"
    )["decision_policy"] == "fixed_delay"

    decision = expand_paper_policy_matrix({**base, "paper_experiment": {
        "dimension": "decision", "admission_policies": ["p90"],
        "reclamation_policies": ["hybrid"],
        "decision_policies": ["eager", "fixed_delay", "predicted_pressure_aware"],
        "restore_policies": ["reactive", "prefetch"],
    }})
    assert len(decision) == 6
    assert {arm["reclamation_policy"] for arm in decision} == {"hybrid"}

    with pytest.raises(ValueError, match="4096"):
        expand_paper_policy_matrix({
            "resources": {"tool_memory_mib": 2048},
            "paper_experiment": {
                "dimension": "spatial", "admission_policies": ["p90"],
            },
        })


def test_per_tool_plan_requires_heterogeneous_reservations_and_keeps_fixed_capacity():
    payload = {
        "tenant_id": "tenant-a", "repo_fingerprint": "org/repo", "generation": 4,
        "pair_digest": "a" * 64, "source_digest": "b" * 64,
        "artifact_count": 20, "clawtune_revision": "c" * 40,
        "prediction": {"latency_p90_sec": 1, "cpu_p90_cores": 1,
                       "memory_p90_bytes": 1024**2, "evidence_count": 10},
        "per_tool_memory": {"workloads": {"rec-a": {
            "reservation_distinct_mib": [322, 384, 401],
            "reservation_distinct_kib": [329728, 393216, 410624],
            "selected_vm_size_class_mib": 2048,
        }}},
    }
    raw = {
        "source": {"repository": "org/repo"},
        "sizing_policies": ["p90_reservation"],
        "resources": {"tool_memory_mib": 4096},
        "p90_reservation": {"prediction": payload, "use_per_tool_memory_plan": True,
                            "workload_name": "rec-a"},
    }
    prediction = _p90_prediction(raw, None)
    assert prediction is not None
    assert _predictive_tool_memory_mib(raw, prediction) == 4096
    payload["per_tool_memory"]["workloads"]["rec-a"]["reservation_distinct_kib"] = [329728]
    with pytest.raises(ValueError, match="heterogeneous"):
        _predictive_tool_memory_mib(raw, prediction)


def test_per_tool_plan_rejects_size_class_above_fixed_capacity():
    payload = {
        "tenant_id": "tenant-a", "repo_fingerprint": "org/repo", "generation": 4,
        "pair_digest": "a" * 64, "source_digest": "b" * 64,
        "artifact_count": 20, "clawtune_revision": "c" * 40,
        "prediction": {"latency_p90_sec": 1, "cpu_p90_cores": 1,
                       "memory_p90_bytes": 1024**2, "evidence_count": 10},
        "per_tool_memory": {"workloads": {"rec-a": {
            "incremental_p90_distinct_kib": [1024, 2048],
            "selected_vm_size_class_mib": 4096,
        }}},
    }
    raw = {
        "source": {"repository": "org/repo"},
        "sizing_policies": ["p90_reservation"],
        "resources": {"tool_memory_mib": 2048},
        "p90_reservation": {"prediction": payload, "use_per_tool_memory_plan": True,
                            "workload_name": "rec-a"},
    }
    prediction = _p90_prediction(raw, None)
    assert prediction is not None
    with pytest.raises(ValueError, match="fixed Tool-VM capacity"):
        _predictive_tool_memory_mib(raw, prediction)


def test_result_envelope_and_failure_categories_serialize_distinctly():
    workflow = expand_matrix(production_spec())[0]
    envelope = ResultEnvelope(run_id="run-1", case_id="case-1", baseline=workflow.baseline,
        classification=WorkflowClassification.PRODUCTION, resolved_workflow=workflow,
        status=RunStatus.SUCCEEDED)
    assert json.loads(envelope.model_dump_json())["status"] == "succeeded"
    assert failure_category_for(RunStatus.TIMED_OUT) == FailureCategory.TIMEOUT
    assert failure_category_for(RunStatus.FAILED, "OOM killed") == FailureCategory.OOM
    assert failure_category_for(RunStatus.FAILED, "admission denied") == FailureCategory.ADMISSION
    assert failure_category_for(RunStatus.FAILED, "sandbox unavailable") == FailureCategory.SANDBOX
    assert failure_category_for(RunStatus.FAILED, "validation failed") == FailureCategory.VALIDATION
    with pytest.raises(ValidationError, match="requires a failure_category"):
        ResultEnvelope(run_id="run-2", case_id="case-2", baseline=workflow.baseline,
            classification=WorkflowClassification.PRODUCTION, resolved_workflow=workflow,
            status=RunStatus.FAILED)


def test_production_envelope_records_actual_concurrency_and_case_image():
    class Core:
        def read_namespace(self, _name):
            return SimpleNamespace()

        def read_namespaced_secret(self, _name, _namespace):
            return SimpleNamespace(data={key: "encoded" for key in (
                "llm-api-key", "llm-upstream-base-url", "llm-model", "openclaw-model-ref",
            )})

    class Node:
        def read_runtime_class(self, name):
            return SimpleNamespace(handler=name, overhead=SimpleNamespace(pod_fixed={"cpu": "250m"}))

    class Custom:
        def list_namespaced_custom_object(self, *_args, **_kwargs):
            return {"items": []}

        def create_namespaced_custom_object(self, *_args, **_kwargs):
            return {}

        def get_namespaced_custom_object(self, *_args, **_kwargs):
            return {"status": {"phase": "Cleaned", "outcome": "Succeeded"}}

    result = KubernetesBenchmarkLauncher(Core(), Custom(), Node()).run(
        [BenchmarkTask("case-1", DIGEST, "fix it")], parallelism=8,
        namespace="clawbox-benchmarks", llm_secret="clawbox-llm",
        llm_egress_cidr="8.8.8.8/32", llm_egress_cidrs=["8.8.8.8/32"],
        runtime_class="kata-fc-arm64", profile="small", timeout_seconds=120,
        command_timeout_seconds=60, run_id="run-1", tenant_id="benchmark",
    )[0]
    workflow = result["result_envelope"]["resolved_workflow"]
    assert workflow["execution"]["concurrency"] == 8
    assert workflow["sandbox"]["tool_image"] == DIGEST


def test_experiment_cli_validates_and_resolves_without_starting_a_runner(tmp_path, capsys):
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(production_spec().model_dump(mode="json")), encoding="utf-8")
    assert experiments_main(["validate", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert experiments_main(["resolve", str(path)]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["sandbox_backend"] == "kubernetes"
    assert resolved["admission_policy"] == "fixed_profile"
    assert resolved["execution"]["concurrency"] == 1


def test_openclaw_adapter_keeps_legacy_mode_alias():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--help"], check=True, capture_output=True, text=True,
    )
    assert "--mode {resident,snapshot}" in completed.stdout
    assert "--residency-policy {resident,llm_wait_checkpoint}" in completed.stdout
    assert "--checkpoint-scope {pair,tool}" in completed.stdout
    assert "--resident-memory-budget-mib" not in completed.stdout


def test_direct_tool_init_reconstructs_guest_collector_environment():
    script = (
        Path(__file__).parents[1] / "scripts" / "experiment-tool-init.sh"
    ).read_text(encoding="utf-8")
    for required in (
        "CLAWTUNE_GUEST_COLLECTOR_HELPER",
        "CLAWTUNE_GUEST_COLLECTOR_PYTHON",
        "CLAWTUNE_GUEST_COLLECTOR_SOCKET",
        "CLAWTUNE_GUEST_ARTIFACT_ROOT",
        "BCC_KERNEL_SOURCE",
        "/testbed/.clawbox/tool-resource",
    ):
        assert required in script


def test_openclaw_completion_poll_tolerates_a_torn_serial_log_line(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_experiment", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    log = tmp_path / "runtime.log"
    log.write_text('{"ok":true,"openclaw_exit_code":"', encoding="utf-8")
    assert module.complete(log) == (False, None)
    log.write_text('{"ok":true,"openclaw_exit_code":0}\n', encoding="utf-8")
    assert module.complete(log) == (True, 0)


def test_openclaw_pair_checkpoint_and_restore_dependency_order():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_experiment_order", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = []

    class Lifecycle:
        def __init__(self, name):
            self.name = name

        def checkpoint_and_evict(self):
            events.append(f"checkpoint-{self.name}")
            return 1.0

        def restore(self):
            events.append(f"restore-{self.name}")
            return 1.0

    runtime = Lifecycle("runtime")
    tool = Lifecycle("tool")
    assert module.checkpoint_runtime_tool_pair(runtime, tool) == 2.0
    assert module.restore_tool_runtime_pair(
        tool, runtime, lambda: events.append("tool-ready")
    ) == 2.0
    assert events == [
        "checkpoint-runtime", "checkpoint-tool",
        "restore-tool", "tool-ready", "restore-runtime",
    ]


def test_idle_checkpoint_victim_selection_is_deterministic_lru():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_lru", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = module.LruIdleSandboxRegistry(
        eviction_policy="eager",
        fixed_delay_s=0,
        checkpoint_break_even_s=0,
        pressure_active=lambda: False,
    )
    noop = lambda: None
    registry.register(module.IdleSandboxCandidate(4, "newer", 20.0, 30.0, noop))
    registry.register(module.IdleSandboxCandidate(2, "older-high-id", 10.0, 30.0, noop))
    registry.register(module.IdleSandboxCandidate(1, "older-low-id", 10.0, 30.0, noop))

    assert registry.select_lru(21.0).request_id == "older-low-id"
    registry.unregister(1, "different-request")
    assert registry.select_lru(21.0).request_id == "older-low-id"
    registry.unregister(1, "older-low-id")
    assert registry.select_lru(21.0).request_id == "older-high-id"


def test_idle_checkpoint_victim_selection_honors_delay_and_pressure():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_lru_policy", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    candidate = module.IdleSandboxCandidate(0, "request", 100.0, 12.0, lambda: None)
    delayed = module.LruIdleSandboxRegistry(
        eviction_policy="fixed_delay",
        fixed_delay_s=5,
        checkpoint_break_even_s=0,
        pressure_active=lambda: False,
    )
    delayed.register(candidate)
    assert delayed.select_lru(104.9) is None
    assert delayed.select_lru(105.0) == candidate

    pressure = threading.Event()
    predicted = module.LruIdleSandboxRegistry(
        eviction_policy="predicted_pressure_aware",
        fixed_delay_s=0,
        checkpoint_break_even_s=10,
        pressure_active=pressure.is_set,
    )
    predicted.register(candidate)
    assert predicted.select_lru(103.0) is None
    pressure.set()
    assert predicted.select_lru(103.0) == candidate


def test_pair_checkpoint_coordinator_blocks_response_until_pair_is_restored():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_pair_race", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = []
    runtime_checkpoint_started = threading.Event()
    allow_checkpoint = threading.Event()

    class Lifecycle:
        def __init__(self, name, rss):
            self.name, self.rss, self.resident = name, rss, True

        def rss_bytes(self):
            return self.rss if self.resident else 0

        def checkpoint_and_evict(self):
            events.append(f"checkpoint-{self.name}")
            if self.name == "runtime":
                runtime_checkpoint_started.set()
                assert allow_checkpoint.wait(1)
            self.resident = False
            return 0.01

        def restore(self):
            events.append(f"restore-{self.name}")
            self.resident = True
            return 0.01

    runtime, tool = Lifecycle("runtime", 700), Lifecycle("tool", 100)
    coordinator = module.PairCheckpointCoordinator(
        runtime, tool, lambda: events.append("tool-ready"),
    )
    coordinator.start_request()
    transitions = []
    eviction = threading.Thread(target=lambda: transitions.append(
        coordinator.evict(lambda: {"numa_memory_bytes": 1, "cgroup_memory_bytes": 2})
    ))
    eviction.start()
    assert runtime_checkpoint_started.wait(1)

    restored = []
    response = threading.Thread(target=lambda: (
        coordinator.begin_response_delivery(), restored.append(coordinator.restore())
    ))
    response.start()
    response.join(0.02)
    assert response.is_alive()
    allow_checkpoint.set()
    eviction.join(1)
    response.join(1)

    assert not eviction.is_alive() and not response.is_alive()
    assert coordinator.state == "running"
    assert transitions[0].runtime_rss_before_bytes == 700
    assert transitions[0].runtime_rss_after_bytes == 0
    assert restored[0]["runtime_restored"] is True
    assert events == [
        "checkpoint-runtime", "checkpoint-tool",
        "restore-tool", "tool-ready", "restore-runtime",
    ]


def test_pair_checkpoint_coordinator_response_wins_before_eviction():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_pair_cancel", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Lifecycle:
        resident = True

        def rss_bytes(self):
            return 1

        def checkpoint_and_evict(self):
            raise AssertionError("response should cancel checkpoint")

    coordinator = module.PairCheckpointCoordinator(
        Lifecycle(), Lifecycle(), lambda: None,
    )
    coordinator.start_request()
    coordinator.begin_response_delivery()
    assert coordinator.evict(lambda: {}) is None
    assert coordinator.restore() is None
    assert coordinator.state == "running"


def test_tool_only_checkpoint_scope_is_an_explicit_ablation():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_tool_scope", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = []

    class Lifecycle:
        def __init__(self, name):
            self.name, self.resident = name, True

        def rss_bytes(self):
            return 10 if self.resident else 0

        def checkpoint_and_evict(self):
            events.append(f"checkpoint-{self.name}")
            self.resident = False
            return 0

        def restore(self):
            events.append(f"restore-{self.name}")
            self.resident = True
            return 0


    runtime, tool = Lifecycle("runtime"), Lifecycle("tool")
    coordinator = module.PairCheckpointCoordinator(
        runtime, tool, lambda: events.append("tool-ready"), checkpoint_scope="tool",
    )
    coordinator.start_request()
    transition = coordinator.evict(lambda: {})
    assert transition is not None and transition.scope == "tool"
    assert runtime.resident and not tool.resident
    coordinator.begin_response_delivery()
    restored = coordinator.restore()
    assert restored is not None and restored["runtime_restored"] is False
    assert events == ["checkpoint-tool", "restore-tool", "tool-ready"]


def test_pair_restore_is_guarded_by_measured_pre_eviction_rss():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_restore_memory", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = []

    class Lifecycle:
        def __init__(self, name, rss):
            self.name, self.rss, self.resident = name, rss, True

        def rss_bytes(self):
            return self.rss if self.resident else 0

        def checkpoint_and_evict(self):
            self.resident = False
            return 0

        def restore(self):
            events.append(f"restore-{self.name}")
            self.resident = True
            return 0

    runtime, tool = Lifecycle("runtime", 700), Lifecycle("tool", 100)
    coordinator = module.PairCheckpointCoordinator(runtime, tool, lambda: None)
    coordinator.start_request()
    assert coordinator.evict(lambda: {}) is not None

    def admission(total_bytes, tool_bytes, operation):
        events.append(("admit", total_bytes, tool_bytes))
        operation()
        events.append(("release", total_bytes, tool_bytes))

    restored = coordinator.restore(admission)
    assert restored["restore_total_reservation_bytes"] == 800
    assert restored["restore_tool_reservation_bytes"] == 100
    assert events == [
        ("admit", 800, 100),
        "restore-tool",
        "restore-runtime",
        ("release", 800, 100),
    ]


def test_pair_checkpoint_coordinator_restores_exactly_once_under_prefetch_race():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_restore_race", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = []
    restore_started = threading.Event()
    allow_restore = threading.Event()

    class Lifecycle:
        def __init__(self, name):
            self.name, self.resident = name, True

        def rss_bytes(self):
            return 10 if self.resident else 0

        def checkpoint_and_evict(self):
            self.resident = False
            return 0

        def restore(self):
            events.append(f"restore-{self.name}")
            if self.name == "tool":
                restore_started.set()
                assert allow_restore.wait(1)
            self.resident = True
            return 0

    runtime, tool = Lifecycle("runtime"), Lifecycle("tool")
    coordinator = module.PairCheckpointCoordinator(
        runtime, tool, lambda: events.append("tool-ready"),
    )
    coordinator.start_request()
    assert coordinator.evict(lambda: {}) is not None
    outcomes = []
    prefetch = threading.Thread(target=lambda: outcomes.append(coordinator.restore()))
    reactive = threading.Thread(target=lambda: outcomes.append(coordinator.restore()))
    prefetch.start()
    assert restore_started.wait(1)
    reactive.start()
    reactive.join(0.02)
    assert reactive.is_alive()
    allow_restore.set()
    prefetch.join(1)
    reactive.join(1)

    assert events == ["restore-tool", "tool-ready", "restore-runtime"]
    assert sum(item is not None for item in outcomes) == 1
    assert coordinator.state == "running"


def test_pair_checkpoint_coordinator_fails_closed_after_partial_checkpoint():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_partial_failure", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Lifecycle:
        def __init__(self, fail=False):
            self.fail, self.resident = fail, True

        def rss_bytes(self):
            return 10 if self.resident else 0

        def checkpoint_and_evict(self):
            if self.fail:
                raise RuntimeError("injected checkpoint failure")
            self.resident = False
            return 0

    runtime, tool = Lifecycle(), Lifecycle(fail=True)
    coordinator = module.PairCheckpointCoordinator(runtime, tool, lambda: None)
    coordinator.start_request()
    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        coordinator.evict(lambda: {})
    assert coordinator.state == "failed"
    coordinator.begin_response_delivery()
    with pytest.raises(RuntimeError, match="checkpoint coordinator failed"):
        coordinator.restore()


def test_openclaw_tool_checkpoint_failure_still_closes_both_vms():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_experiment_failure", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = []

    class Lifecycle:
        def __init__(self, name, fail_checkpoint=False):
            self.name = name
            self.fail_checkpoint = fail_checkpoint

        def checkpoint_and_evict(self):
            events.append(f"checkpoint-{self.name}")
            if self.fail_checkpoint:
                raise RuntimeError("injected Tool snapshot failure")
            return 1.0

        def close(self):
            events.append(f"close-{self.name}")

    runtime = Lifecycle("runtime")
    tool = Lifecycle("tool", fail_checkpoint=True)
    with pytest.raises(RuntimeError, match="injected Tool snapshot failure"):
        try:
            module.checkpoint_runtime_tool_pair(runtime, tool)
        finally:
            module.close_runtime_tool_pair(runtime, tool)

    assert events == [
        "checkpoint-runtime", "checkpoint-tool", "close-runtime", "close-tool",
    ]


def test_openclaw_collects_actual_tool_working_set_and_joins_prediction(tmp_path, monkeypatch):
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_experiment_memory", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    command = "pytest -q"
    digest = __import__("hashlib").sha256(command.encode()).hexdigest()
    bridge = {"execution_id": "exec-1", "command_sha256": digest}
    resource = {"execution_id": "exec-1", "memory_rss_peak_bytes": 64 * 1024**2,
                "sampling_quality": "valid"}
    stdout = (
        "__CLAWBOX_BRIDGE__\n" + json.dumps(bridge) + "\n"
        "__CLAWBOX_CGROUP__\n" + json.dumps(resource) + "\n"
    ).encode()
    monkeypatch.setattr(module, "ssh_capture", lambda *_args: (0, stdout, b"", False))
    events = [{"model_step": 2, "reservation_mib": 384,
               "tool_invocations": [{"command_sha256": digest,
                                     "predicted_command_memory_p90_mib": 80.0}]}]
    rows = module.collect_tool_working_sets({}, events, tmp_path / "actual.out")
    assert rows[0]["actual_command_peak_memory_bytes"] == 64 * 1024**2
    assert rows[0]["prediction_error_mib"] == 16.0
    assert rows[0]["prediction_covered_actual"] is True


def test_openclaw_joins_transformed_commands_by_admission_window(tmp_path, monkeypatch):
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_window_join", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bridges = [
        {"execution_id": "exec-a", "command_sha256": "transformed-a",
         "execution_source": "runtime-envelope"},
        {"execution_id": "exec-b", "command_sha256": "transformed-b",
         "execution_source": "runtime-envelope"},
    ]
    resources = [
        {"execution_id": "exec-a", "memory_rss_peak_bytes": 20 * 1024**2,
         "sampling_quality": "valid", "ts_start": 101.2, "ts_end": 101.8,
         "memory_source": "procfs-process-tree", "monitor_source": "cgroup-v2"},
        {"execution_id": "exec-b", "memory_rss_peak_bytes": 30 * 1024**2,
         "sampling_quality": "valid", "ts_start": 101.4, "ts_end": 101.9,
         "memory_source": "procfs-process-tree", "monitor_source": "cgroup-v2"},
    ]
    stdout = (
        "__CLAWBOX_BRIDGE__\n" + "\n".join(map(json.dumps, bridges))
        + "\n__CLAWBOX_CGROUP__\n" + "\n".join(map(json.dumps, resources)) + "\n"
    ).encode()
    monkeypatch.setattr(module, "ssh_capture", lambda *_args: (0, stdout, b"", False))
    events = [{"model_step": 3, "acquired_elapsed_s": 1.0,
               "released_elapsed_s": 2.0, "reservation_mib": 64.0,
               "predicted_incremental_p90_mib": 64.0,
               "tool_invocations": [{"command_sha256": "raw-command"}]}]
    rows = module.collect_tool_working_sets(
        {}, events, tmp_path / "window.out", arm_started_unix_s=100.0,
    )
    assert rows[0]["join_method"] == "admission_time_window"
    assert rows[0]["actual_execution_count"] == 2
    assert rows[0]["actual_command_peak_memory_bytes"] == 50 * 1024**2
    assert rows[0]["prediction_error_mib"] == 14.0
    assert rows[0]["prediction_covered_actual"] is True


def test_openclaw_joins_bridge_execs_by_order_when_guest_clock_lags(tmp_path, monkeypatch):
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_order_join", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bridges = [
        {"execution_id": f"exec-{index}", "command_sha256": f"wrapped-{index}",
         "execution_source": "runtime-envelope"}
        for index in range(3)
    ]
    resources = [
        {"execution_id": f"exec-{index}", "memory_rss_peak_bytes": (index + 1) * 1024**2,
         "sampling_quality": "valid", "ts_start": 100.1 + index,
         "ts_end": 100.2 + index, "memory_source": "procfs-process-tree",
         "monitor_source": "cgroup-v2"}
        for index in range(3)
    ]
    stdout = (
        "__CLAWBOX_BRIDGE__\n" + "\n".join(map(json.dumps, bridges))
        + "\n__CLAWBOX_CGROUP__\n" + "\n".join(map(json.dumps, resources)) + "\n"
    ).encode()
    monkeypatch.setattr(module, "ssh_capture", lambda *_args: (0, stdout, b"", False))
    events = [
        {"model_step": 1, "acquired_elapsed_s": 10.0, "released_elapsed_s": 11.0,
         "reservation_mib": 8.0, "tool_invocations": [
             {"tool_name": "read"}, {"tool_name": "exec"},
         ]},
        {"model_step": 2, "acquired_elapsed_s": 12.0, "released_elapsed_s": 13.0,
         "reservation_mib": 8.0, "tool_invocations": [
             {"tool_name": "exec"}, {"tool_name": "exec"},
         ]},
    ]
    rows = module.collect_tool_working_sets(
        {}, events, tmp_path / "ordered.out", arm_started_unix_s=100.0,
    )
    assert [row["join_method"] for row in rows] == [
        "ordered_runtime_envelope", "ordered_runtime_envelope",
    ]
    assert [row["actual_execution_count"] for row in rows] == [1, 2]
    assert rows[0]["actual_command_peak_memory_bytes"] == 1 * 1024**2
    assert rows[1]["actual_command_peak_memory_bytes"] == 3 * 1024**2


def test_predictive_batch_reservation_sums_concurrent_tool_calls():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_batch_plan", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = {"per_tool_memory": {"command_headroom_fraction": 0.25,
        "workloads": {"rec-a": {"tool_invocations": [
            {"model_step": 1, "incremental_p90_kib": 100},
            {"model_step": 1, "incremental_p90_kib": 200},
            {"model_step": 2, "predicted_command_memory_p90_mib": 1.0},
        ]}}}}
    steps = module.predictive_steps_from_plan(payload, "rec-a")
    assert steps[1]["incremental_p90_kib"] == 300
    assert steps[2]["incremental_p90_kib"] == 1280


def test_authoritative_cgroup_accounting_skips_unused_rss_fallback(monkeypatch):
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_cgroup_fast_path", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Lifecycle:
        def rss_bytes(self):
            raise AssertionError("RSS fallback must not run for a live cgroup")

    monkeypatch.setattr(module, "cgroup_value", lambda _path, _name: 123)
    assert module.authoritative_cgroup_or_rss(
        Path("/delegated"), [Lifecycle()], threading.Lock(),
    ) == 123


def test_balloon_adjustment_waits_for_guest_and_records_rss_reclamation(monkeypatch):
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_balloon", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Tool:
        def __init__(self):
            self.stats = iter([
                {"target_mib": 1536, "actual_mib": 1400},
                {"target_mib": 1536, "actual_mib": 1536},
            ])
            self.rss = iter([900 * 1024**2, 500 * 1024**2])

        def rss_bytes(self):
            return next(self.rss)

        def set_balloon_target_mib(self, target):
            assert target == 1536
            return next(self.stats)

        def balloon_statistics(self):
            return next(self.stats)

    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    event = module.adjust_balloon(Tool(), 1536, "tool_end_idle_reclaim", 1.0)
    assert event["target_reached"] is True
    assert event["statistics"]["actual_mib"] == 1536
    assert event["tool_firecracker_rss_released_bytes"] == 400 * 1024**2


def test_balloon_materialization_release_requires_guest_target_and_low_host_rss():
    script = Path(__file__).parents[1] / "scripts" / "run-openclaw-experiment.py"
    spec = importlib.util.spec_from_file_location("run_openclaw_balloon_gate", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    verified, limit = module.balloon_materialization_reclaim_verified(
        {"target_reached": True,
         "tool_firecracker_rss_after_bytes": 326 * 1024**2},
        256,
    )
    assert (verified, limit) == (True, 384)
    assert module.balloon_materialization_reclaim_verified(
        {"target_reached": False,
         "tool_firecracker_rss_after_bytes": 100 * 1024**2},
        256,
    )[0] is False
    assert module.balloon_materialization_reclaim_verified(
        {"target_reached": True,
         "tool_firecracker_rss_after_bytes": 385 * 1024**2},
        256,
    )[0] is False
