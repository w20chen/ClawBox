from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
import pytest

from clawbox import cli


def legacy_base_with_idle_reclaim_costs(tmp_path: Path) -> Path:
    raw = yaml.safe_load(Path(
        "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml"
    ).read_text(encoding="utf-8"))
    raw["resources"]["idle_reclaim_costs"] = {
        "provenance": "independent-checkpoint-benchmark-v1",
        "warm_checkpoint_seconds_per_gib": 0.5,
        "warm_restore_seconds_per_gib": 0.5,
        "cold_checkpoint_seconds_per_gib": 1.0,
        "cold_restore_seconds_per_gib": 1.0,
    }
    path = tmp_path / "legacy-with-idle-costs.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
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


def test_public_cli_contains_only_experiment_group() -> None:
    parser = cli.parser()
    groups = next(action for action in parser._actions if action.dest == "group")
    assert set(groups.choices) == {"experiment"}
    commands = next(action for action in groups.choices["experiment"]._actions
                    if action.dest == "command")
    assert set(commands.choices) == {
        "baselines", "configure", "describe", "validate", "plan", "run", "status",
        "setup", "doctor", "qualify", "resume", "abort", "destroy", "collect",
        "trace", "import-trace", "import-corpus", "train", "images", "report", "launch", "host", "recipes", "schema",
        "prove-template-sharing",
    }


def test_prove_template_sharing_clears_future_artifact_before_host_check(
    monkeypatch, tmp_path,
) -> None:
    import clawbox.experiments.qualification as qualification
    from clawbox.experiments.spec import ExperimentSpec

    raw = yaml.safe_load(Path(
        "examples/experiments/six-dimension.yaml"
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

    spec_path = Path("examples/experiments/getting-started.yaml")
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
            spec=Path("examples/experiments/getting-started.yaml"),
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
        "examples/experiments/getting-started.yaml",
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
        cli.launch_experiment(Namespace(spec=Path('examples/experiments/getting-started.yaml'),
            output_root=tmp_path, run_id='apply-failure', apply_host=host_path))
    assert calls == ['inputs', 'apply']


def test_launch_existing_active_returns_status_without_requalification(
    monkeypatch, tmp_path, capsys,
) -> None:
    from argparse import Namespace
    import clawbox.experiments.inputs as inputs
    import clawbox.experiments.qualification as qualification
    import clawbox.experiments.supervisor as supervisor

    spec_path = Path("examples/experiments/getting-started.yaml")
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

    spec_path = Path("examples/experiments/getting-started.yaml")
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


def test_validate_and_plan_v2(capsys) -> None:
    path = "examples/experiments/getting-started.yaml"
    assert cli.main(["experiment", "validate", path]) == 0
    assert '"valid": true' in capsys.readouterr().out
    assert cli.main(["experiment", "plan", path]) == 0
    assert '"arms"' in capsys.readouterr().out


def test_baseline_catalog_is_available_from_public_cli(capsys) -> None:
    assert cli.main(["experiment", "baselines"]) == 0
    output = capsys.readouterr().out
    assert "fixed-resident" in output
    assert "predicted-resident" in output
    assert "predicted-wait-snapshot" in output
    assert "tool-full-resident" not in output
    assert "DEPRECATED" not in output
    assert "compare only within a listed family" in output
    assert "no_control" not in output
    assert "snapshot_scope=agent_and_tool" in output
    assert "release_ranking=measured_cost" not in output

    assert cli.main(["experiment", "baselines", "--all"]) == 0
    all_output = capsys.readouterr().out
    assert "DEPRECATED" not in all_output
    assert "prediction-only-resident" not in all_output


def test_public_workflow_accepts_independent_runtime_ablation(capsys) -> None:
    path = "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml"
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


def test_configure_and_describe_experiment_without_running_vms(tmp_path, capsys) -> None:
    output = tmp_path / "friendly.yaml"
    base = "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml"
    assert cli.main([
        "experiment", "configure", base, str(output),
        "--profile", str(tmp_path / "no-host-profile.json"),
        "--experiment-id", "friendly",
        "--concurrency", "1,5,60",
        "--runtime-memory-gib", "1",
        "--tool-memory-gib", "2",
        "--tool-workspace", "/testbed",
        "--runtime-template-id", "runtime-1g",
        "--runtime-image-reference", "registry/runtime-1g",
        "--runtime-image-digest", "sha256:" + "a" * 64,
        "--tool-template-id", "tool-2g",
        "--tool-image-reference", "registry/tool-2g",
        "--tool-image-digest", "sha256:" + "b" * 64,
        "--pool-memory-gib", "32",
        "--prediction-artifact", "/data/frozen-p50.json",
        "--baseline", "fixed-resident",
        "--baseline", "predicted-resident",
    ]) == 0
    configured = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert configured["experiment_id"] == "friendly"
    assert configured["execution"]["concurrency_levels"] == [1, 5, 60]
    assert configured["runtime"]["memory_mib"] == 1024
    assert configured["sandbox"]["memory_mib"] == 2048
    assert configured["sandbox"]["workspace"] == "/testbed"
    assert configured["resources"]["pool_memory_budget_mib"] == 32768
    assert configured["resources"]["prediction_artifact"] == "/data/frozen-p50.json"
    assert [item["name"] for item in configured["policies"]] == [
        "fixed-resident", "predicted-resident",
    ]
    rendered = capsys.readouterr().out
    assert "c60: 120 VMs" in rendered
    assert "5.625x (offered configured capacity exceeds policy budget)" in rendered

    assert cli.main(["experiment", "describe", str(output), "--json"]) == 0
    overview = json.loads(capsys.readouterr().out)
    assert overview["pair_memory_gib"] == 3
    assert overview["concurrency"][-1]["offered_provisioned_capacity_exceeds_policy_budget"] is True
    assert overview["arm_count"] == 6
    assert overview["admission"]["policy_budget_gib"] == 32
    assert overview["admission"]["static"]["per_command_mib"] == 256


def test_configure_can_use_validated_training_p90_for_static_admission(
    tmp_path, capsys,
) -> None:
    artifact = tmp_path / "p50.json"
    artifact.write_text(json.dumps({
        "schema": "clawbox_prediction_v2",
        "repository": "owner/repo",
        "training_validated": True,
        "static_tool_memory_calibration": {
            "metric": "host_cgroup_memory_current_increment",
            "recommended_mib": 347,
        },
    }), encoding="utf-8")
    output = tmp_path / "auto-static.yaml"

    assert cli.main([
        "experiment", "configure", "examples/experiments/getting-started.yaml",
        str(output), "--repository", "owner/repo",
        "--prediction-artifact", str(artifact),
        "--static-tool-memory-mib", "auto",
    ]) == 0
    capsys.readouterr()
    configured = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert configured["resources"]["static_tool_memory_mib"] == 347


def test_configure_refuses_to_replace_existing_output(tmp_path, capsys) -> None:
    output = tmp_path / "existing.yaml"
    output.write_text("keep", encoding="utf-8")
    base = "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml"
    assert cli.main(["experiment", "configure", base, str(output)]) == 1
    assert output.read_text(encoding="utf-8") == "keep"
    assert "--force" in capsys.readouterr().err


def test_configure_uses_the_setup_host_profile(tmp_path, capsys) -> None:
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    profile = tmp_path / "host.json"
    profile.write_text(json.dumps({
        "node": "node-a",
        "runtime": {
            "template_id": "runtime-current",
            "source_image_reference": "registry/runtime@" + digest_a,
            "image_digest": digest_a,
            "memory_mib": 2048,
            "vcpu": 2,
        },
        "sandbox": {
            "template_id": "tool-current",
            "source_image_reference": "registry/tool@" + digest_b,
            "image_digest": digest_b,
            "memory_mib": 4096,
            "vcpu": 2,
        },
        "local_memory_cgroup": "/sys/fs/cgroup/cube_sandbox/sandbox",
        "local_memory_capacity_mib": 65536,
        "local_numa_node": 0,
        "warm_root": "/mnt/clawbox-warm",
        "warm_capacity_mib": 131072,
        "warm_numa_node": 1,
    }), encoding="utf-8")
    output = tmp_path / "profiled.yaml"

    assert cli.main([
        "experiment", "configure", "examples/experiments/getting-started.yaml",
        str(output), "--profile", str(profile), "--snapshot-storage", "warm-only",
    ]) == 0
    capsys.readouterr()
    configured = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert configured["runtime"]["template_id"] == "runtime-current"
    assert configured["sandbox"]["template_id"] == "tool-current"
    assert configured["resources"]["target_node"] == "node-a"
    assert configured["resources"]["local_memory_capacity_mib"] == 65536
    assert configured["resources"]["warm_memory_capacity_mib"] == 131072
    assert configured["resources"]["warm_snapshot_root"] == "/mnt/clawbox-warm"


def test_configure_wait_checkpoint_needs_no_llm_prediction_or_idle_costs(tmp_path, capsys):
    output = tmp_path / "actual-wait.yaml"
    assert cli.main([
        "experiment", "configure",
        "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml",
        str(output), "--baseline", "predicted-wait-snapshot",
        "--prediction-artifact", "/data/frozen-p50.json",
    ]) == 0
    configured = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert not any("model_wait_prediction" in k for k in configured["inference"]["configuration"])
    assert configured["policies"][0]["runtime_snapshot"] is True


def test_llm_duration_prediction_option_is_not_supported(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["experiment", "configure",
                  "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml",
                  str(tmp_path / "invalid.yaml"), "--model-wait-prediction-seconds", "auto"])
    assert exc.value.code != 0
    assert "unrecognized arguments" in capsys.readouterr().err


def test_legacy_baseline_selection_requires_one_comparison_family(
    tmp_path, capsys,
) -> None:
    output = tmp_path / "cross-family.yaml"
    assert cli.main([
        "experiment", "configure",
        "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml",
        str(output), "--baseline", "default-resident",
        "--baseline", "predicted-resident",
    ]) == 1
    assert not output.exists()
    assert "do not share a comparison family" in capsys.readouterr().err

    assert cli.main([
        "experiment", "configure",
        "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml",
        str(output), "--baseline", "uncontrolled-resident",
        "--baseline", "predicted-resident",
        "--prediction-artifact", "/data/frozen-p50.json",
    ]) == 0


def test_configure_rejects_vm_shape_change_without_new_template(
    tmp_path, capsys,
) -> None:
    output = tmp_path / "wrong-shape.yaml"
    base = "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml"
    assert cli.main([
        "experiment", "configure", base, str(output),
        "--runtime-memory-gib", "1",
    ]) == 1
    assert not output.exists()
    assert "requires a new Runtime template" in capsys.readouterr().err


def test_configure_explicit_shape_wins_over_host_profile(
    tmp_path, capsys,
) -> None:
    output = tmp_path / "wrong-shape.yaml"
    profile = tmp_path / "host.json"
    profile.write_text(json.dumps({
        "runtime": {
            "template_id": "tpl-runtime",
            "source_image_reference": "registry/runtime:current",
            "image_digest": "sha256:" + "a" * 64,
            "vcpu": 2,
            "memory_mib": 2048,
        },
        "sandbox": {
            "template_id": "tpl-tool",
            "source_image_reference": "registry/tool:current",
            "image_digest": "sha256:" + "b" * 64,
            "vcpu": 2,
            "memory_mib": 4096,
        },
    }), encoding="utf-8")

    assert cli.main([
        "experiment", "configure",
        "examples/experiments/getting-started.yaml", str(output),
        "--profile", str(profile),
        "--runtime-memory-gib", "1",
    ]) == 1
    assert not output.exists()
    assert "requires a new Runtime template" in capsys.readouterr().err
