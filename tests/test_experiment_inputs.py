from experiment_fixtures import worker_fixture
import json
import json
from pathlib import Path

import pytest
import yaml

from clawbox import cli
from clawbox.experiments.inputs import (
    inspect_trace, validate_inputs, validate_launch_inputs,
)
from clawbox.experiments.spec import load_experiment


EXAMPLE = Path("tests/fixtures/experiments/getting-started.yaml")


def test_documented_example_validates_without_host(capsys):
    assert cli.main(["experiment", "validate", "--baseline", "examples/baselines/neutral.yaml", "--workload", "examples/workloads/smoke.yaml", "--inputs"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["inputs"]["traces"][0]["model_calls"] == 2
    assert result["armCount"] == 1












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

    monkeypatch.setenv("OPENCLAW_MODEL_REF", "must-not-supply-missing-configuration")
    monkeypatch.setattr(
        worker, "ExperimentWorker",
        lambda *a, **k: pytest.fail("worker constructed"),
    )
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["inference"]["configuration"].pop("model")
    path = tmp_path / "missing-model.yaml"
    from clawbox.experiments.configuration import dump_experiment
    from clawbox.experiments.spec import ExperimentSpec
    path.write_text(dump_experiment(ExperimentSpec.model_validate(raw)), encoding="utf-8")

    assert cli.main(["experiment", "run", str(path)]) == 1
    assert "OpenClaw requires inference.configuration.model" in capsys.readouterr().err


def test_duplicate_action_ids_are_rejected(tmp_path):
    path = tmp_path / "duplicate.jsonl"
    row = Path("examples/traces/smoke.jsonl").read_text().splitlines()[0]
    path.write_text(row + "\n" + row + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        inspect_trace(path)


def test_launch_reuses_saved_trace_validation_while_explicit_audit_rehashes(
    tmp_path, monkeypatch,
):
    import clawbox.experiments.inputs as inputs

    trace = tmp_path / "ready.jsonl"
    trace.write_bytes(Path("examples/traces/smoke.jsonl").read_bytes())
    saved = inspect_trace(trace)
    spec = worker_fixture(EXAMPLE)
    case = spec.workload.cases[0].model_copy(update={
        "source_reference": str(trace),
        "replay_trace_reference": str(trace),
        "metadata": {"input_validation": saved},
    })
    spec = spec.model_copy(update={
        "workload": spec.workload.model_copy(update={"cases": (case,)}),
    })
    monkeypatch.setattr(
        inputs, "inspect_trace",
        lambda _path: pytest.fail("normal launch reparsed a prevalidated trace"),
    )

    fast = validate_launch_inputs(spec)["traces"][0]

    assert fast["sha256"] == saved["sha256"]
    assert fast["identity_source"] == "workload.metadata.input_validation"
    assert fast["content_rehashed"] is False

    monkeypatch.undo()
    trace.write_text(trace.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    audited = validate_inputs(spec)["traces"][0]
    assert audited["sha256"] != saved["sha256"]


def test_report_uses_same_result_root(tmp_path, capsys):
    root = tmp_path / "run-a"
    root.mkdir()
    (root / "summary.md").write_text("# Result\n", encoding="utf-8")
    assert cli.main(["--output-root", str(tmp_path), "experiment", "report", "run-a"]) == 0
    assert capsys.readouterr().out == "# Result\n"








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
    assert 'cannot read configuration' in capsys.readouterr().err
