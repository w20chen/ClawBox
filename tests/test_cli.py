from __future__ import annotations

from types import SimpleNamespace

from clawbox import cli


def test_submit_cli_forwards_full_problem_and_tenant(monkeypatch, tmp_path, capsys):
    problem = tmp_path / "problem.txt"
    problem.write_text("Fix the parser.\n", encoding="utf-8")
    seen = {}

    class FakeClient:
        def __init__(self, base_url, *, token, tenant_id):
            seen.update(base_url=base_url, token=token, tenant_id=tenant_id)

        def create_run(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                run_id="01TEST",
                phase="Accepted",
                idempotency_replay=False,
                current_attempt_id=None,
            )

    monkeypatch.setattr(cli, "ManagedAPIClient", FakeClient)
    monkeypatch.setenv("CLAWBOX_TOKEN", "secret-token")
    assert cli.main([
        "--api-url", "http://api.example",
        "--tenant", "team-a",
        "submit",
        "--input-ref", "repo-a",
        "--problem-file", str(problem),
        "--idempotency-key", "request-1",
    ]) == 0

    assert seen["tenant_id"] == "team-a"
    assert seen["problem_statement"] == "Fix the parser.\n"
    assert seen["input_sha256"] == cli.input_sha256("Fix the parser.\n")
    assert '"runId": "01TEST"' in capsys.readouterr().out


def test_status_cli_uses_tenant_scope(monkeypatch, capsys):
    seen = {}

    class FakeClient:
        def __init__(self, base_url, *, token, tenant_id):
            seen["tenant_id"] = tenant_id

        def get_run(self, run_id):
            return {"runId": run_id, "phase": "Succeeded"}

    monkeypatch.setattr(cli, "ManagedAPIClient", FakeClient)
    monkeypatch.setenv("CLAWBOX_TOKEN", "secret-token")
    assert cli.main(["--tenant", "team-b", "status", "01RUN"]) == 0
    assert seen["tenant_id"] == "team-b"
    assert '"phase": "Succeeded"' in capsys.readouterr().out


def test_submit_cli_generates_idempotency_key_when_omitted(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, base_url, *, token, tenant_id):
            pass

        def create_run(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                run_id="01TEST",
                phase="Accepted",
                idempotency_replay=False,
                current_attempt_id=None,
            )

    monkeypatch.setattr(cli, "ManagedAPIClient", FakeClient)
    monkeypatch.setenv("CLAWBOX_TOKEN", "secret-token")
    assert cli.main(["submit", "--input-ref", "repo-a"]) == 0
    assert seen["idempotency_key"].startswith("cli-")
