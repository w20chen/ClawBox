from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from clawbox import cli


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
        "trace", "import-trace", "train", "images", "report", "launch",
    }


def test_launch_qualifies_when_needed_then_starts_run(
    monkeypatch, tmp_path,
) -> None:
    from argparse import Namespace
    import clawbox.experiments.inputs as inputs
    import clawbox.experiments.qualification as qualification

    spec_path = Path("examples/experiments/getting-started.yaml")
    receipt = tmp_path / "qualification.json"
    calls: list[object] = []
    monkeypatch.setattr(inputs, "validate_inputs", lambda spec: calls.append("inputs"))
    monkeypatch.setattr(
        qualification, "require_host_ready",
        lambda spec: calls.append("host"),
    )
    monkeypatch.setattr(
        qualification, "validate_receipt",
        lambda spec, path: (_ for _ in ()).throw(ValueError("stale")),
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
        qualification=receipt,
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
    assert "tool-static-resident" in output
    assert "tool-p50-resident" in output
    assert "tool-p50-wait-reactive" in output
    assert "tool-full-resident" not in output
    assert "DEPRECATED" not in output

    assert cli.main(["experiment", "baselines", "--all"]) == 0
    all_output = capsys.readouterr().out
    assert "tool-full-resident [DEPRECATED]" in all_output
    assert "tool-p50-wait-proactive [DEPRECATED]" in all_output


def test_public_workflow_rejects_legacy_policy_names(capsys) -> None:
    path = "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml"
    assert cli.main(["experiment", "validate", path]) == 1
    assert "not supported by the standalone workflow" in capsys.readouterr().err


def test_status_and_collect_read_standalone_results(tmp_path, capsys) -> None:
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
    assert cli.main([*prefix, "collect", "run-1"]) == 0
    assert '"arm_id": "arm-a"' in capsys.readouterr().out


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


def test_configure_and_describe_experiment_without_running_vms(tmp_path, capsys) -> None:
    output = tmp_path / "friendly.yaml"
    base = "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml"
    assert cli.main([
        "experiment", "configure", base, str(output),
        "--experiment-id", "friendly",
        "--concurrency", "1,5,60",
        "--runtime-memory-gib", "1",
        "--tool-memory-gib", "2",
        "--runtime-template-id", "runtime-1g",
        "--runtime-image-reference", "registry/runtime-1g",
        "--runtime-image-digest", "sha256:" + "a" * 64,
        "--tool-template-id", "tool-2g",
        "--tool-image-reference", "registry/tool-2g",
        "--tool-image-digest", "sha256:" + "b" * 64,
        "--pool-memory-gib", "32",
        "--prediction-artifact", "/data/frozen-p50.json",
        "--baseline", "tool-static-resident",
        "--baseline", "tool-p50-resident",
    ]) == 0
    configured = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert configured["experiment_id"] == "friendly"
    assert configured["execution"]["concurrency_levels"] == [1, 5, 60]
    assert configured["runtime"]["memory_mib"] == 1024
    assert configured["sandbox"]["memory_mib"] == 2048
    assert configured["resources"]["pool_memory_budget_mib"] == 32768
    assert configured["resources"]["prediction_artifact"] == "/data/frozen-p50.json"
    assert [item["name"] for item in configured["policies"]] == [
        "tool-static-resident", "tool-p50-resident",
    ]
    rendered = capsys.readouterr().out
    assert "c60: 120 VMs" in rendered
    assert "5.625x (overcommit)" in rendered

    assert cli.main(["experiment", "describe", str(output), "--json"]) == 0
    overview = json.loads(capsys.readouterr().out)
    assert overview["pair_memory_gib"] == 3
    assert overview["concurrency"][-1]["memory_overcommit"] is True
    assert overview["arm_count"] == 6
    assert overview["admission"]["policy_budget_gib"] == 32
    assert overview["admission"]["static"]["per_command_mib"] == 256


def test_configure_can_use_validated_training_p90_for_static_admission(
    tmp_path, capsys,
) -> None:
    artifact = tmp_path / "p50.json"
    artifact.write_text(json.dumps({
        "schema": "clawbox_p50_v1",
        "repository": "owner/repo",
        "training_validated": True,
        "static_tool_memory_calibration": {
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


def test_configure_requires_wait_prediction_for_wait_aware_policy(
    tmp_path, capsys,
) -> None:
    output = tmp_path / "wait-aware.yaml"
    base = "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml"
    command = [
        "experiment", "configure", base, str(output),
        "--baseline", "tool-p50-wait-reactive",
    ]
    assert cli.main(command) == 1
    assert not output.exists()
    assert "model-wait-prediction-seconds" in capsys.readouterr().err

    assert cli.main([
        *command,
        "--prediction-artifact", "/data/frozen-p50.json",
        "--model-wait-prediction-seconds", "3",
        "--model-wait-prediction-source", "separate-recording-v1",
    ]) == 0
    configured = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert configured["inference"]["configuration"][
        "model_wait_prediction_source"
    ] == "separate-recording-v1"


def test_configure_can_freeze_scaled_trace_median_model_wait(tmp_path, capsys) -> None:
    output = tmp_path / "auto-wait.yaml"
    base = "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml"

    assert cli.main([
        "experiment", "configure", base, str(output),
        "--baseline", "tool-p50-wait-reactive",
        "--prediction-artifact", "/data/frozen-p50.json",
        "--model-wait-prediction-seconds", "auto",
    ]) == 0
    capsys.readouterr()
    configuration = yaml.safe_load(output.read_text(encoding="utf-8"))[
        "inference"
    ]["configuration"]
    assert configuration["model_wait_prediction_seconds"] > 0
    assert configuration["model_wait_prediction_source"].startswith(
        "recorded-trace-median-scaled:"
    )


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
