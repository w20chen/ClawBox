from __future__ import annotations

import json
import subprocess
import sys
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
from clawbox.replay.study import study_experiment_spec


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


def test_capability_rules_do_not_claim_future_or_legacy_paths_are_implemented():
    with pytest.raises(CapabilityError, match="p90_elastic"):
        expand_matrix(production_spec(scheduling={"baselines": ["p90-elastic"]}))
    with pytest.raises(CapabilityError, match="legacy Tool-only"):
        expand_matrix(production_spec(scheduling={"baselines": ["p90-static"]}))
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
