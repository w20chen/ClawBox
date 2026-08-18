"""M1-4 API tests: idempotency, tenant scoping, cancel/retry, concurrency."""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from clawbox.api.app import create_app
from clawbox.api.templates import TemplateRegistry
from clawbox.managed.db import ManagedBase, RunRow

TOKEN = "test-token"
TEMPLATES = {
    "swe-rebench-arm64": {
        "1": {
            "toolImage": "127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:" + "a" * 64,
            "secretName": "swe-rebench-secret",
            "runtimeImage": "127.0.0.1:5000/clawbox/runtime-arm64:dev",
            "llmEgressCIDR": "0.0.0.0/0",
            "profile": "small",
            "maxDeadlineSeconds": 3600,
            "minDeadlineSeconds": 60,
        }
    }
}


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    ManagedBase.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(engine, expire_on_commit=False)
    app = create_app(
        session_factory=factory,
        registry=TemplateRegistry.from_dict(TEMPLATES),
        service_token=TOKEN,
    )
    with TestClient(app) as client:
        yield client, factory
    engine.dispose()


HEADERS = {"X-Clawbox-Token": TOKEN, "X-Tenant-Id": "tenant-a"}


def body(**overrides):
    base = dict(
        projectId="proj-1",
        templateRef="swe-rebench-arm64",
        templateRevision=1,
        inputRef="15five__scim2-filter-parser-13",
        inputSha256="a" * 64,
        deadlineSeconds=1800,
        idempotencyKey="key-1",
    )
    base.update(overrides)
    return base


def test_create_run_201(client):
    c, _ = client
    r = c.post("/v1/runs", json=body(), headers=HEADERS)
    assert r.status_code == 201
    data = r.json()
    assert data["phase"] == "Accepted"
    assert data["idempotencyReplay"] is False
    assert len(data["runId"]) == 26


def test_idempotent_replay_returns_same_run(client):
    c, _ = client
    first = c.post("/v1/runs", json=body(), headers=HEADERS).json()
    second = c.post("/v1/runs", json=body(), headers=HEADERS)
    assert second.status_code == 200
    data = second.json()
    assert data["runId"] == first["runId"]
    assert data["idempotencyReplay"] is True


def test_same_key_different_body_409(client):
    c, _ = client
    c.post("/v1/runs", json=body(), headers=HEADERS)
    r = c.post("/v1/runs", json=body(projectId="proj-2"), headers=HEADERS)
    assert r.status_code == 409


def test_unknown_template_422(client):
    c, _ = client
    r = c.post("/v1/runs", json=body(templateRef="not-a-template"), headers=HEADERS)
    assert r.status_code == 422


def test_deadline_out_of_range_422(client):
    c, _ = client
    r = c.post("/v1/runs", json=body(deadlineSeconds=999999), headers=HEADERS)
    assert r.status_code == 422


def test_auth_required(client):
    c, _ = client
    assert c.post("/v1/runs", json=body(), headers={"X-Tenant-Id": "tenant-a"}).status_code == 401
    assert c.post("/v1/runs", json=body(), headers={"X-Clawbox-Token": TOKEN}).status_code == 400


def test_get_run_tenant_scoped(client):
    c, _ = client
    run_id = c.post("/v1/runs", json=body(), headers=HEADERS).json()["runId"]
    ok = c.get(f"/v1/runs/{run_id}", headers=HEADERS)
    assert ok.status_code == 200
    assert ok.json()["tenantId"] == "tenant-a"
    other = c.get(
        f"/v1/runs/{run_id}",
        headers={"X-Clawbox-Token": TOKEN, "X-Tenant-Id": "tenant-b"},
    )
    assert other.status_code == 404


def test_cancel_sets_desired_state(client):
    c, _ = client
    run_id = c.post("/v1/runs", json=body(), headers=HEADERS).json()["runId"]
    r = c.post(f"/v1/runs/{run_id}/cancel", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["desiredState"] == "Cancelled"


def test_retry_creates_new_attempt(client):
    c, _ = client
    run_id = c.post("/v1/runs", json=body(), headers=HEADERS).json()["runId"]
    first = c.post(f"/v1/runs/{run_id}/retry", headers=HEADERS)
    assert first.status_code == 200
    second = c.post(f"/v1/runs/{run_id}/retry", headers=HEADERS)
    assert second.status_code == 200
    assert first.json()["attemptId"] != second.json()["attemptId"]
    assert first.json()["attemptNumber"] == 1
    assert second.json()["attemptNumber"] == 2
    attempts = c.get(f"/v1/runs/{run_id}/attempts", headers=HEADERS).json()
    assert len(attempts) == 2


def test_events_are_ordered(client):
    c, _ = client
    run_id = c.post("/v1/runs", json=body(), headers=HEADERS).json()["runId"]
    c.post(f"/v1/runs/{run_id}/retry", headers=HEADERS)
    events = c.get(f"/v1/runs/{run_id}/events", headers=HEADERS).json()
    assert [e["sequence"] for e in events] == [1, 2]
    assert [e["eventType"] for e in events] == ["run.accepted", "attempt.created"]


def test_api_cannot_reference_arbitrary_secret(client):
    # The request body has no secret field at all: the Secret name is fixed by
    # the template registry, never user input.
    c, _ = client
    assert "secretName" not in body()
    assert "llmSecretName" not in body()
    r = c.post("/v1/runs", json=body(), headers=HEADERS)
    assert r.status_code == 201


def test_twenty_concurrent_same_idempotency_key_yields_one_run(client):
    _, factory = client
    results: list[int] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def submit():
        from sqlalchemy.orm import Session

        app = create_app(
            session_factory=factory,
            registry=TemplateRegistry.from_dict(TEMPLATES),
            service_token=TOKEN,
        )
        with TestClient(app) as c:
            r = c.post("/v1/runs", json=body(), headers=HEADERS)
            with lock:
                results.append(r.status_code)
                if r.status_code >= 400:
                    errors.append(RuntimeError(r.text))

    threads = [threading.Thread(target=submit) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert results.count(201) == 1
    assert results.count(200) == 19

    with factory() as db:
        rows = db.scalars(select(RunRow)).all()
        assert len(rows) == 1
