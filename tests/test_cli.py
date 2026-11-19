from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
import pytest

from clawbox import cli
from experiment_fixtures import worker_fixture
from clawbox.experiments.configuration import dump_experiment

def frozen_example(tmp_path, name="getting-started.yaml"):
    path = tmp_path / name
    path.write_text(dump_experiment(worker_fixture(Path("tests/fixtures/experiments") / name)), encoding="utf-8")
    return path




def test_images_subprocess_can_import_repository_package(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_call(command, *, env):
        captured["command"] = command
        captured["env"] = env
        return 0

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    assert cli.main([
        "experiment", "images", "--profile", str(tmp_path / "host.json"),
    ]) == 0

    repository = str(Path(cli.__file__).resolve().parents[1])
    assert captured["command"][1] == str(
        Path(repository) / "scripts" / "refresh-lab-images.py"
    )
    assert str(captured["env"]["PYTHONPATH"]).split(os.pathsep)[0] == repository




def test_prove_template_sharing_clears_future_artifact_before_host_check(
    monkeypatch, tmp_path,
) -> None:
    import clawbox.experiments.qualification as qualification
    from clawbox.experiments.spec import ExperimentSpec

    raw = yaml.safe_load(Path(
        "tests/fixtures/experiments/six-dimension.yaml"
    ).read_text(encoding="utf-8"))
    raw["evidence"]["template_base_sharing_artifact"] = str(
        tmp_path / "future-proof.json"
    )
    spec = ExperimentSpec.model_validate(raw)
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(cli, "load_experiment", lambda _path: spec)
    monkeypatch.setattr(
        qualification, "require_host_ready",
        lambda observed: calls.append((
            "host", observed.resources.template_base_sharing_artifact,
        )),
    )
    monkeypatch.setattr(
        qualification, "template_base_sharing_test",
        lambda observed, **_kwargs: calls.append((
            "proof", observed.resources.template_base_sharing_artifact,
        )) or {"passed": True},
    )
    monkeypatch.setattr(
        qualification, "write_template_base_sharing_artifact",
        lambda observed, _result, **_kwargs: calls.append((
            "write", observed.resources.template_base_sharing_artifact,
        )) or {"template_count": 1, "backend_identity": {}},
    )

    assert cli.main([
        "experiment", "prove-template-sharing",
        "ignored.yaml", "--output", str(tmp_path / "proof.json"),
        "--artifact-root", str(tmp_path / "proof-raw"),
        "--resume-result", str(tmp_path / "prior-result.json"),
    ]) == 0
    assert calls == [("host", None), ("proof", None), ("write", None)]


def test_launch_qualifies_when_needed_then_starts_run(
    monkeypatch, tmp_path,
) -> None:
    from argparse import Namespace
    import clawbox.experiments.inputs as inputs
    import clawbox.experiments.qualification as qualification

    spec_path = frozen_example(tmp_path)
    receipt = tmp_path / "qualification.json"
    calls: list[object] = []
    monkeypatch.setattr(
        inputs, "validate_launch_inputs", lambda spec: calls.append("inputs"),
    )
    monkeypatch.setattr(
        qualification, "require_host_ready",
        lambda spec: calls.append("host"),
    )
    monkeypatch.setattr(
        qualification, "validate_receipt",
        lambda spec, path: (_ for _ in ()).throw(ValueError("stale")),
    )
    monkeypatch.setattr(
        qualification, "default_receipt_path", lambda _path: receipt,
    )
    monkeypatch.setattr(
        qualification, "qualify",
        lambda spec, **kwargs: calls.append(("qualify", kwargs)) or {"status": "succeeded"},
    )
    monkeypatch.setattr(
        cli, "main",
        lambda argv=None: calls.append(("main", argv)) or 0,
    )

    assert cli.launch_experiment(Namespace(
        spec=spec_path,
        output_root=tmp_path,
        qualification=None,
        qualification_concurrency=4,
        force_qualify=False,
        run_id="self-service",
        detach=True,
    )) == 0
    assert calls[:2] == ["inputs", "host"]
    assert calls[2][0] == "qualify"
    assert calls[2][1]["receipt_path"] == receipt
    assert calls[2][1]["concurrency"] == 4
    assert calls[3] == (
        "main",
        [
            "--output-root", str(tmp_path), "experiment", "run",
            str(spec_path), "--run-id", "self-service",
            "--qualification", str(receipt), "--detach",
        ],
    )


def test_launch_rejects_an_invalid_explicit_receipt_without_requalifying(
    monkeypatch, tmp_path,
) -> None:
    from argparse import Namespace
    import clawbox.experiments.qualification as qualification

    receipt = tmp_path / "qualification.json"
    monkeypatch.setattr(
        qualification, "validate_receipt",
        lambda spec, path: (_ for _ in ()).throw(ValueError("contract mismatch")),
    )
    monkeypatch.setattr(
        qualification, "qualify",
        lambda *_args, **_kwargs: pytest.fail("unexpected qualification"),
    )

    with pytest.raises(ValueError, match="explicit qualification receipt cannot be reused"):
        cli.launch_experiment(Namespace(
            spec=frozen_example(tmp_path),
            output_root=tmp_path,
            qualification=receipt,
            qualification_concurrency=None,
            force_qualify=False,
            run_id="explicit-stale-receipt",
            detach=True,
        ))


def test_qualify_cli_delegates_preflight_and_input_validation_to_qualify(
    monkeypatch, tmp_path, capsys,
) -> None:
    import clawbox.experiments.qualification as qualification

    calls = []
    receipt = tmp_path / "qualification.json"
    monkeypatch.setattr(
        qualification,
        "qualify",
        lambda spec, **kwargs: calls.append(kwargs) or {"status": "succeeded"},
    )

    assert cli.main([
        "--output-root", str(tmp_path), "experiment", "qualify",
        str(frozen_example(tmp_path)),
        "--concurrency", "2", "--receipt", str(receipt),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert calls == [{
        "output_base": tmp_path,
        "receipt_path": receipt,
        "concurrency": 2,
    }]


def test_launch_host_apply_failure_stops_before_qualification(monkeypatch, tmp_path):
    from argparse import Namespace
    from clawbox.experiments import host, inputs, qualification
    calls = []
    host_path = tmp_path/'host.yaml'
    host.init_config(host_path)
    monkeypatch.setattr(inputs, 'validate_inputs', lambda spec: calls.append('inputs'))
    def apply(config, profile):
        assert profile == host_path.with_suffix('.json')
        calls.append('apply')
        raise RuntimeError('host apply failed')
    monkeypatch.setattr(host, 'apply', apply)
    monkeypatch.setattr(qualification, 'require_host_ready', lambda spec: pytest.fail('continued after failed apply'))
    with pytest.raises(RuntimeError, match='host apply failed'):
        cli.launch_experiment(Namespace(spec=frozen_example(tmp_path),
            output_root=tmp_path, run_id='apply-failure', apply_host=host_path))
    assert calls == ['inputs', 'apply']


def test_launch_existing_active_returns_status_without_requalification(
    monkeypatch, tmp_path, capsys,
) -> None:
    from argparse import Namespace
    import clawbox.experiments.inputs as inputs
    import clawbox.experiments.qualification as qualification
    import clawbox.experiments.supervisor as supervisor

    spec_path = frozen_example(tmp_path)
    run_root = tmp_path / "already-running"
    run_root.mkdir()
    (run_root / "experiment.yaml").write_text(
        spec_path.read_text(encoding="utf-8"), encoding="utf-8",
    )
    monkeypatch.setattr(
        inputs, "validate_inputs",
        lambda spec: (_ for _ in ()).throw(AssertionError("inputs revalidated")),
    )
    monkeypatch.setattr(
        qualification, "require_host_ready",
        lambda spec: (_ for _ in ()).throw(AssertionError("host rechecked")),
    )
    monkeypatch.setattr(
        supervisor, "status_for_run",
        lambda path: {"state": "running", "supervisor_alive": True},
    )

    assert cli.launch_experiment(Namespace(
        spec=spec_path,
        output_root=tmp_path,
        qualification=None,
        qualification_concurrency=None,
        force_qualify=False,
        run_id="already-running",
        apply_host=tmp_path/'must-not-be-read.yaml',
        detach=False,
    )) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "running"
    assert output["message"] == "run is already active"


def test_launch_existing_success_skips_host_and_qualification(
    monkeypatch, tmp_path, capsys,
) -> None:
    from argparse import Namespace
    import clawbox.experiments.inputs as inputs
    import clawbox.experiments.qualification as qualification
    import clawbox.experiments.reporting as reporting
    import clawbox.experiments.supervisor as supervisor

    spec_path = frozen_example(tmp_path)
    run_root = tmp_path / "already-done"
    run_root.mkdir()
    (run_root / "experiment.yaml").write_text(
        spec_path.read_text(encoding="utf-8"), encoding="utf-8",
    )
    report = run_root / "report.md"
    report.write_text("existing report\n", encoding="utf-8")
    monkeypatch.setattr(
        inputs, "validate_inputs",
        lambda spec: (_ for _ in ()).throw(AssertionError("inputs revalidated")),
    )
    monkeypatch.setattr(
        qualification, "require_host_ready",
        lambda spec: (_ for _ in ()).throw(AssertionError("host rechecked")),
    )
    monkeypatch.setattr(
        qualification, "validate_receipt",
        lambda spec, path: (_ for _ in ()).throw(AssertionError("receipt rechecked")),
    )
    monkeypatch.setattr(
        supervisor, "status_for_run",
        lambda path: {"state": "succeeded", "supervisor_alive": False},
    )
    monkeypatch.setattr(
        reporting, "write_run_report_artifacts", lambda path: (report, None),
    )

    assert cli.launch_experiment(Namespace(
        spec=spec_path,
        output_root=tmp_path,
        qualification=None,
        qualification_concurrency=None,
        force_qualify=False,
        run_id="already-done",
        detach=False,
    )) == 0
    assert capsys.readouterr().out == "existing report\n"


def test_validate_and_plan_v2(capsys, tmp_path) -> None:
    path = str(frozen_example(tmp_path))
    assert cli.main(["experiment", "validate", path]) == 0
    assert '"valid": true' in capsys.readouterr().out
    assert cli.main(["experiment", "plan", path]) == 0
    assert '"arms"' in capsys.readouterr().out




def test_public_workflow_accepts_independent_runtime_ablation(capsys, tmp_path) -> None:
    path = str(frozen_example(tmp_path, "openclaw-cube-replay-c60-overcommit.yaml"))
    assert cli.main(["experiment", "validate", path]) == 0
    assert '"valid": true' in capsys.readouterr().out


def test_status_reads_standalone_result_state(tmp_path, capsys) -> None:
    run = tmp_path / "run-1"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps([
        {"arm": {"arm_id": "arm-a"}, "status": "succeeded"},
    ]), encoding="utf-8")
    (run / "run-state.json").write_text(json.dumps({
        "schema_version": 1, "run_id": "run-1", "state": "succeeded",
        "arms": {"arm-a": {"status": "succeeded"}},
    }), encoding="utf-8")
    prefix = ["--output-root", str(tmp_path), "experiment"]
    assert cli.main([*prefix, "status", "run-1"]) == 0
    assert '"state": "succeeded"' in capsys.readouterr().out


def test_status_reads_current_wrapped_summary(tmp_path, capsys) -> None:
    run = tmp_path / "run-current"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({
        "run_id": "run-current",
        "arms": [{"arm": {"arm_id": "arm-b"}, "status": "succeeded"}],
    }), encoding="utf-8")
    (run / "run-state.json").write_text(json.dumps({
        "schema_version": 1, "run_id": "run-current", "state": "succeeded",
        "arms": {"arm-b": {"status": "succeeded"}},
    }), encoding="utf-8")
    prefix = ["--output-root", str(tmp_path), "experiment"]
    assert cli.main([*prefix, "status", "run-current"]) == 0
    output = capsys.readouterr().out
    assert '"arm-b"' in output
    assert '"status": "succeeded"' in output


def test_collect_expands_compact_arm_result_reference(tmp_path, capsys) -> None:
    run = tmp_path / "run-compact"
    (run / "arms").mkdir(parents=True)
    result = {
        "arm": {"arm_id": "arm-c"}, "status": "failed",
        "performance": {"duration_seconds": 12.5},
    }
    (run / "arms" / "arm-c.json").write_text(json.dumps(result), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({
        "schema_version": 2, "run_id": "run-compact",
        "arms": [{
            "arm": {"arm_id": "arm-c"}, "status": "failed",
            "result": "arms/arm-c.json",
        }],
    }), encoding="utf-8")
    prefix = ["--output-root", str(tmp_path), "experiment"]

    assert cli.main([*prefix, "collect", "run-compact"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["arms"] == [result]
