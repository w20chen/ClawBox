from __future__ import annotations

from types import SimpleNamespace

from clawbox import cli


def test_public_cli_contains_only_experiment_group() -> None:
    parser = cli.parser()
    groups = next(action for action in parser._actions if action.dest == "group")
    assert set(groups.choices) == {"experiment"}
    commands = next(action for action in groups.choices["experiment"]._actions
                    if action.dest == "command")
    assert set(commands.choices) == {"validate", "plan", "run", "status", "cancel", "collect"}


def test_validate_and_plan_v2(capsys) -> None:
    path = "examples/experiments/vertical-slice.yaml"
    assert cli.main(["experiment", "validate", path]) == 0
    assert '"valid": true' in capsys.readouterr().out
    assert cli.main(["experiment", "plan", path]) == 0
    assert '"arms"' in capsys.readouterr().out


def test_run_and_status_use_managed_api(monkeypatch, capsys) -> None:
    seen = {}

    class FakeClient:
        def __init__(self, base_url, *, token, tenant_id):
            seen["tenant"] = tenant_id
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def create_experiment(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(run_id="run-1", phase="accepted", idempotency_replay=False)
        def get_run(self, run_id): return {"runId": run_id, "phase": "succeeded"}

    monkeypatch.setattr(cli, "ManagedAPIClient", FakeClient)
    monkeypatch.setenv("CLAWBOX_TOKEN", "token")
    spec = "examples/experiments/vertical-slice.yaml"
    assert cli.main(["--tenant", "team-a", "experiment", "run", spec]) == 0
    assert seen["tenant"] == "team-a" and seen["experiment_spec"]["schema_version"] == 2
    assert cli.main(["experiment", "status", "run-1"]) == 0
    assert '"phase": "succeeded"' in capsys.readouterr().out
