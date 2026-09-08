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
    assert result["inputs"]["traces"][0]["model_calls"] == 1
    assert result["armCount"] == 1


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
                                validation_command="test -f /workspace/result.txt")
    assert spec.execution.concurrency_levels == (2, 3)
    assert spec.workload.cases[0].repository == "example/project"
    assert spec.workload.cases[0].validation == "test -f /workspace/result.txt"
    assert validate_inputs(spec)["traces"][0]["path"] == str(trace)


def test_missing_trace_is_rejected_before_worker_creation(tmp_path, monkeypatch, capsys):
    import clawbox.experiments.worker as worker
    monkeypatch.setattr(worker, "ExperimentWorker", lambda *a, **k: pytest.fail("worker constructed"))
    spec = configure_experiment(EXAMPLE, trace=str(tmp_path / "missing.jsonl"))
    path = tmp_path / "missing.yaml"
    path.write_text(yaml.safe_dump(spec.model_dump(mode="json")), encoding="utf-8")
    assert cli.main(["experiment", "run", str(path)]) == 1
    assert "missing.jsonl" in capsys.readouterr().err


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


@pytest.mark.parametrize('payload', [{}, {'tool-1': -1}, {'tool-1': float('nan')}, {'tool-1': True}])
def test_managed_predictions_reject_non_command_records(tmp_path, payload):
    source = tmp_path / 'predictions.json'
    source.write_text(json.dumps(payload))
    spec = configure_experiment(EXAMPLE, baseline_names=['tool-p90-resident'], p90_kb=str(source))
    with pytest.raises(ValueError, match='no command records'):
        validate_inputs(spec)


def test_managed_predictions_reject_action_id_dictionary(tmp_path):
    source = tmp_path / 'predictions.json'
    source.write_text('{"tool-1": 256}')
    spec = configure_experiment(EXAMPLE, baseline_names=['tool-p90-resident'], p90_kb=str(source))
    from clawbox.experiments.spec_types import AgentDriver
    spec = spec.model_copy(update={'agent': spec.agent.model_copy(update={'driver': AgentDriver.OPENCLAW})})
    # Revalidate so enums match the worker's actual model.
    from clawbox.experiments.spec import ExperimentSpec
    spec = ExperimentSpec.model_validate(spec.model_dump(mode='json'))
    with pytest.raises(ValueError, match='no command records'):
        validate_inputs(spec)


def test_status_reads_finished_arms_before_summary(tmp_path, capsys):
    root = tmp_path / 'running' / 'arms'
    root.mkdir(parents=True)
    (root / 'one.json').write_text(json.dumps({'arm': {'arm_id': 'one'}, 'status': 'succeeded'}))
    (root / 'writing.json').write_text('{')
    assert cli.main(['--output-root', str(tmp_path), 'experiment', 'status', 'running']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['summaryComplete'] is False
    assert value['arms'] == [{'armId': 'one', 'status': 'succeeded'}]


def test_run_refuses_existing_results_before_worker_creation(tmp_path, monkeypatch, capsys):
    import clawbox.experiments.worker as worker
    monkeypatch.setattr(worker, 'ExperimentWorker', lambda *a, **kw: pytest.fail('worker constructed'))
    (tmp_path / 'old-run').mkdir()
    assert cli.main(['--output-root', str(tmp_path), 'experiment', 'run', str(EXAMPLE), '--run-id', 'old-run']) == 1
    assert 'fresh --run-id' in capsys.readouterr().err


def test_malformed_yaml_has_a_cli_error_not_a_traceback(tmp_path, capsys):
    path = tmp_path / 'broken.yaml'
    path.write_text('workload: [')
    assert cli.main(['experiment', 'validate', str(path)]) == 1
    assert 'cannot read experiment' in capsys.readouterr().err


def test_dimension_filters_select_only_catalog_combinations():
    from clawbox.experiments.preset_view import select_presets
    assert set(select_presets(estimate=['fixed', 'predicted'], idle=['resident', 'immediate'])) == {
        'tool-static-resident', 'tool-p90-resident', 'tool-static-eager-reactive', 'tool-p90-eager-reactive'}
    assert select_presets(['tool-static-resident', 'tool-p90-resident'], estimate=['fixed']) == ['tool-static-resident']
    with pytest.raises(ValueError, match='No implemented preset'):
        select_presets(reserve_during=['session'], estimate=['predicted'])
