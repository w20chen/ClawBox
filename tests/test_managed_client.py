"""M1-7 tests: Managed API client + benchmark launcher as API client."""

import pytest
import httpx

from clawbox.benchmark import managed_client
from clawbox.api.app import create_app
from clawbox.api.templates import TemplateRegistry
from clawbox.benchmark.managed_client import (
    ManagedAPIClient,
    RunConflictError,
    UnknownTemplateError,
    input_sha256,
    submit_benchmark,
)
from clawbox.managed.db import ManagedBase

TOKEN = "test-token"
TEMPLATES = {
    "swe-rebench-arm64": {
        "1": {
            "toolImage": "127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:" + "a" * 64,
            "secretName": "swe-rebench-secret",
            "runtimeImage": "127.0.0.1:5000/clawbox/runtime-arm64:dev",
            "llmEgressCIDR": "0.0.0.0/0",
            "profile": "small",
        }
    }
}


def test_loopback_client_ignores_ambient_proxy_settings(monkeypatch):
    captured = []

    class FakeHTTPClient:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def close(self):
            pass

    monkeypatch.setattr(managed_client.httpx, "Client", FakeHTTPClient)

    local = ManagedAPIClient(
        "http://127.0.0.1:49152", token=TOKEN, tenant_id="tenant-a"
    )
    remote = ManagedAPIClient(
        "https://api.example.test", token=TOKEN, tenant_id="tenant-a"
    )
    local.close()
    remote.close()

    assert captured[0]["trust_env"] is False
    assert captured[1]["trust_env"] is True


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(
        f"sqlite:///{tmp_path / 'client.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    ManagedBase.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    app = create_app(
        session_factory=factory,
        registry=TemplateRegistry.from_dict(TEMPLATES),
        service_token=TOKEN,
    )
    with TestClient(app) as http:
        api = ManagedAPIClient("http://test", token=TOKEN, tenant_id="tenant-a", client=http)
        yield api
    engine.dispose()


def test_create_run_via_client(client):
    r = client.create_run(
        project_id="proj-1",
        template_ref="swe-rebench-arm64",
        template_revision=1,
        input_ref="15five__scim2-filter-parser-13",
        input_sha256="a" * 64,
        deadline_seconds=1800,
        idempotency_key="key-1",
    )
    assert r.idempotency_replay is False
    assert len(r.run_id) == 26


def test_replay_and_conflict_via_client(client):
    kwargs = dict(
        project_id="proj-1",
        template_ref="swe-rebench-arm64",
        template_revision=1,
        input_ref="15five__scim2-filter-parser-13",
        input_sha256="a" * 64,
        deadline_seconds=1800,
        idempotency_key="key-1",
    )
    first = client.create_run(**kwargs)
    replay = client.create_run(**kwargs)
    assert replay.idempotency_replay is True
    assert replay.run_id == first.run_id
    with pytest.raises(RunConflictError):
        client.create_run(**{**kwargs, "project_id": "proj-2"})


def test_unknown_template_via_client(client):
    with pytest.raises(UnknownTemplateError):
        client.create_run(
            project_id="proj-1",
            template_ref="nope",
            template_revision=1,
            input_ref="x",
            input_sha256="a" * 64,
            deadline_seconds=1800,
            idempotency_key="k",
        )


def test_get_cancel_retry_attempts_events(client):
    run = client.create_run(
        project_id="proj-1",
        template_ref="swe-rebench-arm64",
        template_revision=1,
        input_ref="15five__scim2-filter-parser-13",
        input_sha256="a" * 64,
        deadline_seconds=1800,
        idempotency_key="key-2",
    )
    got = client.get_run(run.run_id)
    assert got["phase"] == "Accepted"
    cancelled = client.cancel(run.run_id)
    assert cancelled["desiredState"] == "Cancelled"
    attempt1 = client.retry(run.run_id)
    attempt2 = client.retry(run.run_id)
    assert attempt1["attemptId"] != attempt2["attemptId"]
    attempts = client.list_attempts(run.run_id)
    assert len(attempts) == 2
    events = client.list_events(run.run_id)
    assert [e["eventType"] for e in events] == [
        "run.accepted", "run.cancel_requested", "attempt.created", "attempt.created",
    ]


def test_submit_benchmark_idempotency_prefix(client):
    tasks = [
        {"instance_id": "a", "problem_statement": "fix a"},
        {"instance_id": "b", "problem_statement": "fix b"},
    ]
    first = submit_benchmark(
        client,
        tasks=tasks,
        project_id="benchmark",
        template_ref="swe-rebench-arm64",
        template_revision=1,
        deadline_seconds=1800,
        idempotency_prefix="bench",
    )
    second = submit_benchmark(
        client,
        tasks=tasks,
        project_id="benchmark",
        template_ref="swe-rebench-arm64",
        template_revision=1,
        deadline_seconds=1800,
        idempotency_prefix="bench",
    )
    assert [r.run_id for r in first] == [r.run_id for r in second]
    assert all(r.idempotency_replay for r in second)
    assert len(first) == 2


def test_input_sha256_is_stable():
    assert input_sha256("hello") == input_sha256("hello")
    assert len(input_sha256("hello")) == 64
    assert input_sha256("hello") != input_sha256("world")
