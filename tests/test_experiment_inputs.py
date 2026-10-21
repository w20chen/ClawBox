import json
import json
from pathlib import Path

import pytest
import yaml

from clawbox import cli
from clawbox.experiments.configure import configure_experiment
from clawbox.experiments.inputs import inspect_trace, validate_inputs
from clawbox.experiments.spec import load_experiment


EXAMPLE = Path("examples/experiments/getting-started.yaml")


def test_documented_example_validates_without_host(capsys):
    assert cli.main(["experiment", "validate", str(EXAMPLE), "--inputs"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["inputs"]["traces"][0]["model_calls"] == 2
    assert result["armCount"] == 1


def test_incomplete_tool_request_trace_fails_before_creating_vms(tmp_path):
    path = tmp_path / "truncated.jsonl"
    path.write_text("\n".join(Path("examples/traces/smoke.jsonl").read_text().splitlines()[:2]) + "\n")
    spec = configure_experiment(EXAMPLE, trace=str(path))
    with pytest.raises(ValueError, match="no final model response"):
        validate_inputs(spec)
    spec.inference.configuration["max_model_steps"] = 1
    assert validate_inputs(spec)["traces"][0]["ends_with_tool_request"]


def test_arbitrary_trace_and_case_replace_single_case_and_keep_concurrency(tmp_path):
    trace = tmp_path / "different-project.jsonl"
    trace.write_text(Path("examples/traces/smoke.jsonl").read_text(), encoding="utf-8")
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw["workload"].pop("cases")
    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump(raw), encoding="utf-8")
    spec = configure_experiment(base, trace=str(trace), case_id="different-task",
                                prompt="A different task", repository="example/project",
                                base_commit="revision", concurrency="2,3",
                                session_assignment="single_case",
                                validation_command="test -f /workspace/result.txt")
    assert spec.execution.concurrency_levels == (2, 3)
    assert spec.workload.cases[0].repository == "example/project"
    assert spec.workload.cases[0].validation == "test -f /workspace/result.txt"
    assert validate_inputs(spec)["traces"][0]["path"] == str(trace)


def test_workload_manifest_selects_unique_corpus_without_single_case_overrides(tmp_path):
    manifest = tmp_path / "workload.json"
    trace = tmp_path / "one.replay.jsonl"
    trace.write_text(Path("examples/traces/smoke.jsonl").read_text(), encoding="utf-8")
    manifest.write_text(json.dumps({"tasks": [{
        "case_id": "one", "prompt": "one", "replay_trace_reference": trace.name,
    }]}), encoding="utf-8")
    spec = configure_experiment(EXAMPLE, workload_manifest=str(manifest))
    assert spec.workload.source == "swe_rebench"
    assert spec.workload.input == str(manifest)
    assert spec.workload.session_assignment.value == "unique"
    assert spec.workload.cases == ()
    with pytest.raises(ValueError, match="cannot be combined"):
        configure_experiment(
            EXAMPLE, workload_manifest=str(manifest), trace="one.jsonl",
        )


def test_missing_trace_is_rejected_before_worker_creation(tmp_path, monkeypatch, capsys):
    import clawbox.experiments.worker as worker
    monkeypatch.setattr(worker, "ExperimentWorker", lambda *a, **k: pytest.fail("worker constructed"))
    spec = configure_experiment(EXAMPLE, trace=str(tmp_path / "missing.jsonl"))
    path = tmp_path / "missing.yaml"
    path.write_text(yaml.safe_dump(spec.model_dump(mode="json")), encoding="utf-8")
    assert cli.main(["experiment", "run", str(path)]) == 1
    assert "missing.jsonl" in capsys.readouterr().err


def test_manifest_round_robin_keeps_concurrency_independent_of_pool_size(tmp_path):
    from clawbox.experiments.spec import expand_matrix
    manifest = tmp_path / "workload.json"
    manifest.write_text(json.dumps({"tasks": [
        {"case_id": f"task-{i}", "replay_trace_reference": "trace.jsonl"}
        for i in range(501)
    ]}), encoding="utf-8")
    spec = configure_experiment(EXAMPLE, workload_manifest=str(manifest),
                                session_assignment="round_robin", concurrency="1280")
    arms = expand_matrix(spec)
    assert spec.workload.session_assignment.value == "round_robin"
    assert all(arm.concurrency == 1280 and len(arm.session_cases) == 501 for arm in arms)


def test_round_robin_rejects_single_case_manifest_when_loaded(tmp_path):
    from clawbox.experiments.spec import WorkloadSpec, load_workload_cases
    manifest = tmp_path / "workload.json"
    manifest.write_text(json.dumps({"tasks": [{"case_id": "only"}]}))
    spec = WorkloadSpec(source="swe_rebench", input=str(manifest), session_assignment="round_robin")
    with pytest.raises(ValueError, match="at least two cases"):
        load_workload_cases(spec)


def test_missing_openclaw_model_is_rejected_before_worker_creation(
    tmp_path, monkeypatch, capsys,
):
    import clawbox.experiments.worker as worker

    monkeypatch.delenv("OPENCLAW_MODEL_REF", raising=False)
    monkeypatch.setattr(
        worker, "ExperimentWorker",
        lambda *a, **k: pytest.fail("worker constructed"),
    )
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["inference"]["configuration"].pop("model")
    path = tmp_path / "missing-model.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    assert cli.main(["experiment", "run", str(path)]) == 1
    assert "OpenClaw requires inference.configuration.model" in capsys.readouterr().err


def test_duplicate_action_ids_are_rejected(tmp_path):
    path = tmp_path / "duplicate.jsonl"
    row = Path("examples/traces/smoke.jsonl").read_text().splitlines()[0]
    path.write_text(row + "\n" + row + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        inspect_trace(path)


def test_report_uses_same_result_root(tmp_path, capsys):
    root = tmp_path / "run-a"
    root.mkdir()
    (root / "summary.md").write_text("# Result\n", encoding="utf-8")
    assert cli.main(["--output-root", str(tmp_path), "experiment", "report", "run-a"]) == 0
    assert capsys.readouterr().out == "# Result\n"


def test_validation_override_replaces_case_command(tmp_path):
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw['workload']['cases'][0]['validation'] = 'old-command'
    base = tmp_path / 'base.yaml'
    base.write_text(yaml.safe_dump(raw))
    spec = configure_experiment(base, validation_command='new-command')
    assert spec.validation.command == 'new-command'
    assert spec.workload.cases[0].validation == 'new-command'


def test_p50_admission_requires_a_frozen_training_artifact():
    with pytest.raises(ValueError, match='prediction_artifact'):
        configure_experiment(EXAMPLE, baseline_names=['predicted-resident'])


def test_replay_rejects_unavailable_tools_before_execution(tmp_path):
    trace = tmp_path / 'unsupported.jsonl'
    trace.write_text(Path('examples/traces/smoke.jsonl').read_text().replace(
        '"name":"exec"', '"name":"edit_file"'), encoding='utf-8')
    spec = configure_experiment(EXAMPLE, trace=str(trace))
    with pytest.raises(ValueError, match='replay tools unavailable.*edit_file'):
        validate_inputs(spec)


def test_status_marks_running_state_with_dead_supervisor_as_orphaned(tmp_path, capsys):
    root = tmp_path / 'running'
    root.mkdir(parents=True)
    (root / 'run-state.json').write_text(json.dumps({
        'schema_version': 1,
        'run_id': 'running',
        'state': 'running',
        'supervisor': {'pid': 999_999_999, 'create_time': 0},
        'arms': {'one': {'status': 'succeeded'}},
    }))
    assert cli.main(['--output-root', str(tmp_path), 'experiment', 'status', 'running']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['state'] == 'orphaned'
    assert value['stored_state'] == 'running'
    assert value['supervisor_alive'] is False
    assert value['arms']['one']['status'] == 'succeeded'


def test_run_refuses_existing_results_before_worker_creation(tmp_path, monkeypatch, capsys):
    import clawbox.experiments.worker as worker
    monkeypatch.setattr(worker, 'ExperimentWorker', lambda *a, **kw: pytest.fail('worker constructed'))
    (tmp_path / 'old-run').mkdir()
    assert cli.main(['--output-root', str(tmp_path), 'experiment', 'run', str(EXAMPLE), '--run-id', 'old-run']) == 1
    assert 'experiment resume old-run' in capsys.readouterr().err


def test_malformed_yaml_has_a_cli_error_not_a_traceback(tmp_path, capsys):
    path = tmp_path / 'broken.yaml'
    path.write_text('workload: [')
    assert cli.main(['experiment', 'validate', str(path)]) == 1
    assert 'cannot read experiment' in capsys.readouterr().err


def test_dimension_filters_select_independent_mechanisms():
    from clawbox.experiments.preset_view import select_presets
    assert set(select_presets(admission=['fixed', 'predicted'], reclamation=['resident'],
                              runtime_control=['off'])) == {'fixed-resident', 'predicted-resident'}
    assert select_presets(runtime_control=['off'], admission=['predicted']) == ['predicted-resident', 'predicted-wait-snapshot']
    with pytest.raises(ValueError):
        select_presets(estimate=['predicted'])
