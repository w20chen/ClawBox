from __future__ import annotations

import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from clawbox.api.app import create_app
from clawbox.api.dispatcher import Dispatcher
from clawbox.managed.db import ManagedBase


class FakeCRBackend:
    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}

    def apply_sandboxtask(self, manifest: dict) -> bool:
        name = manifest["metadata"]["name"]
        if name in self.tasks:
            return False
        self.tasks[name] = manifest
        return True

    def cancel_sandboxtask(self, name: str, namespace: str) -> None:
        self.tasks[name]["spec"]["desiredState"] = "Cancelled"

    def list_sandboxtasks(self, namespace: str) -> list[dict]:
        return list(self.tasks.values())


def test_api_outbox_attempt_projects_exactly_one_v2_sandboxtask(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'managed.db'}",
                           connect_args={"check_same_thread": False})
    ManagedBase.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    app = create_app(session_factory=factory, service_token="token")
    spec = yaml.safe_load(open("examples/experiments/vertical-slice.yaml", encoding="utf-8"))
    headers = {"X-Clawbox-Token": "token", "X-Tenant-Id": "tenant-a"}
    with TestClient(app) as client:
        response = client.post("/v1/runs", headers=headers, json={
            "projectId": "project-a", "experimentSpec": spec, "idempotencyKey": "request-a",
        })
        assert response.status_code == 201
        run_id = response.json()["runId"]

        backend = FakeCRBackend()
        dispatcher = Dispatcher(session_factory=factory, cr_backend=backend)
        assert dispatcher.run_once() == 1  # run.accepted -> Attempt
        assert dispatcher.run_once() == 1  # attempt.created -> SandboxTask
        while dispatcher.run_once():
            pass
        assert len(backend.tasks) == 1
        task = next(iter(backend.tasks.values()))
        assert task["apiVersion"] == "clawbox.openai.com/v1alpha2"
        assert task["spec"]["runRef"]["runID"] == run_id
        assert task["spec"]["experimentSpec"]["schema_version"] == 2

        assert client.post(f"/v1/runs/{run_id}/cancel", headers=headers).status_code == 200
        while dispatcher.run_once():
            pass
        assert next(iter(backend.tasks.values()))["spec"]["desiredState"] == "Cancelled"
