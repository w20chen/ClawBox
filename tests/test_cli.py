from __future__ import annotations

import json

from clawbox import cli


def test_public_cli_contains_only_experiment_group() -> None:
    parser = cli.parser()
    groups = next(action for action in parser._actions if action.dest == "group")
    assert set(groups.choices) == {"experiment"}
    commands = next(action for action in groups.choices["experiment"]._actions
                    if action.dest == "command")
    assert set(commands.choices) == {"validate", "plan", "run", "status", "collect"}


def test_validate_and_plan_v2(capsys) -> None:
    path = "examples/experiments/vertical-slice.yaml"
    assert cli.main(["experiment", "validate", path]) == 0
    assert '"valid": true' in capsys.readouterr().out
    assert cli.main(["experiment", "plan", path]) == 0
    assert '"arms"' in capsys.readouterr().out


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
