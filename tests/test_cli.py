from __future__ import annotations

import json

import yaml

from clawbox import cli


def test_public_cli_contains_only_experiment_group() -> None:
    parser = cli.parser()
    groups = next(action for action in parser._actions if action.dest == "group")
    assert set(groups.choices) == {"experiment"}
    commands = next(action for action in groups.choices["experiment"]._actions
                    if action.dest == "command")
    assert set(commands.choices) == {
        "baselines", "configure", "describe", "validate", "plan", "run", "status",
        "collect",
    }


def test_validate_and_plan_v2(capsys) -> None:
    path = "examples/experiments/vertical-slice.yaml"
    assert cli.main(["experiment", "validate", path]) == 0
    assert '"valid": true' in capsys.readouterr().out
    assert cli.main(["experiment", "plan", path]) == 0
    assert '"arms"' in capsys.readouterr().out


def test_baseline_catalog_is_available_from_public_cli(capsys) -> None:
    assert cli.main(["experiment", "baselines"]) == 0
    output = capsys.readouterr().out
    assert "tool-p90-wait-proactive" in output
    assert "resources.p90_predictions" in output
    assert "compatibility-alias" not in output


def test_status_and_collect_read_standalone_results(tmp_path, capsys) -> None:
    run = tmp_path / "run-1"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps([
        {"arm": {"arm_id": "arm-a"}, "status": "succeeded"},
    ]), encoding="utf-8")
    prefix = ["--output-root", str(tmp_path), "experiment"]
    assert cli.main([*prefix, "status", "run-1"]) == 0
    assert '"status": "succeeded"' in capsys.readouterr().out
    assert cli.main([*prefix, "collect", "run-1"]) == 0
    assert '"arm_id": "arm-a"' in capsys.readouterr().out


def test_status_reads_current_wrapped_summary(tmp_path, capsys) -> None:
    run = tmp_path / "run-current"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({
        "run_id": "run-current",
        "arms": [{"arm": {"arm_id": "arm-b"}, "status": "succeeded"}],
    }), encoding="utf-8")
    prefix = ["--output-root", str(tmp_path), "experiment"]
    assert cli.main([*prefix, "status", "run-current"]) == 0
    output = capsys.readouterr().out
    assert '"armId": "arm-b"' in output
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
        "--pool-memory-gib", "32",
        "--baseline", "tool-static-resident",
        "--baseline", "tool-static-eager-reactive",
    ]) == 0
    configured = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert configured["experiment_id"] == "friendly"
    assert configured["execution"]["concurrency_levels"] == [1, 5, 60]
    assert configured["runtime"]["memory_mib"] == 1024
    assert configured["sandbox"]["memory_mib"] == 2048
    assert configured["resources"]["pool_memory_budget_mib"] == 32768
    assert [item["name"] for item in configured["policies"]] == [
        "tool-static-resident", "tool-static-eager-reactive",
    ]
    rendered = capsys.readouterr().out
    assert "c60: 120 VMs" in rendered
    assert "5.625x (overcommit)" in rendered

    assert cli.main(["experiment", "describe", str(output), "--json"]) == 0
    overview = json.loads(capsys.readouterr().out)
    assert overview["pair_memory_gib"] == 3
    assert overview["concurrency"][-1]["memory_overcommit"] is True
    assert overview["arm_count"] == 6


def test_configure_refuses_to_replace_existing_output(tmp_path, capsys) -> None:
    output = tmp_path / "existing.yaml"
    output.write_text("keep", encoding="utf-8")
    base = "examples/experiments/openclaw-cube-replay-c60-overcommit.yaml"
    assert cli.main(["experiment", "configure", base, str(output)]) == 1
    assert output.read_text(encoding="utf-8") == "keep"
    assert "--force" in capsys.readouterr().err
