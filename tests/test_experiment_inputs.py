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
    assert result["inputs"]["traces"][0]["tool_calls"] == 1
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
    with pytest.raises(ValueError, match="unique"):
        inspect_trace(path)


def test_report_uses_same_result_root(tmp_path, capsys):
    root = tmp_path / "run-a"
    root.mkdir()
    (root / "summary.md").write_text("# Result\n", encoding="utf-8")
    assert cli.main(["--output-root", str(tmp_path), "experiment", "report", "run-a"]) == 0
    assert capsys.readouterr().out == "# Result\n"
