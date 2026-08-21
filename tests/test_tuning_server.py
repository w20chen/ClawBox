"""Tests for the tuning KB control-plane API (P1)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from clawbox.common.config import settings
from clawbox.tuning.schema import CollectionQuality, ToolObservation, utcnow
from clawbox.tuning.server import create_app
from clawbox.tuning.validate import sign_observation
from clawbox.tuning.native import sign_native_manifest
from test_native_tuning import make_manifest

TOKEN = settings.service_token
INGEST_SECRET = settings.ingest_secret


def make_obs(
    execution_id: str,
    *,
    command: str = "python -m pytest -q",
    duration_sec: float = 4.0,
    seq: int = 0,
) -> ToolObservation:
    end = utcnow()
    start = end - timedelta(seconds=duration_sec)
    return ToolObservation(
        execution_id=execution_id,
        tool_name="exec",
        command=command,
        command_digest="sha256-" + execution_id[-8:],
        repo_fingerprint="github.com/acme/foo",
        run_id=f"run-{execution_id}",
        sequence_no=seq,
        start_time=start,
        end_time=end,
        duration_sec=duration_sec,
        status_code="ok",
        exit_code=0,
        complete=True,
        collection_quality=CollectionQuality.VALID,
        coverage_ratio=1.0,
        cpu_time_sec=duration_sec * 1.0,
        cpu_utilization_avg_cores=1.0,
        rss_peak_bytes=1024**2,
    )


def auth_headers():
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/server.db")
    return TestClient(app)


def test_native_api_publishes_atomic_pair_and_replays_idempotently(client):
    manifest = make_manifest()
    payload = manifest.model_dump(mode="json", by_alias=True)
    body = {
        "manifest": payload,
        "signature": sign_native_manifest(manifest, INGEST_SECRET),
    }
    first = client.post("/v1/kb/native-batches", json=body, headers=auth_headers())
    assert first.status_code == 200
    assert first.json()["accepted"] is True
    assert first.json()["generation"] == 1

    replay = client.post("/v1/kb/native-batches", json=body, headers=auth_headers())
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert replay.json()["generation"] == 1

    response = client.get(
        "/v1/kb/native-snapshot",
        params={"tenant_id": "tenant-a", "repo": "github.com/acme/foo"},
        headers=auth_headers(),
    )
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["generation"] == 1
    assert snapshot["clause_snapshot"]["schema"] == "runtime_clause_resource_kb_v4"
    assert snapshot["runtime_snapshot"]["schema"] == "runtime_tool_resource_kb_v1"


def test_requires_service_token(client):
    resp = client.get("/v1/kb/generation", params={"tenant_id": "tenant-a", "repo": "github.com/acme/foo"})
    assert resp.status_code == 401
    resp = client.get(
        "/v1/kb/generation",
        params={"tenant_id": "tenant-a", "repo": "github.com/acme/foo"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


def test_generation_starts_at_zero(client):
    resp = client.get(
        "/v1/kb/generation",
        params={"tenant_id": "tenant-a", "repo": "github.com/acme/foo"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["generation"] == 0


def test_post_observations_then_read_snapshot(client):
    obs = [make_obs("api-0001"), make_obs("api-0002")]
    body = {
        "tenant_id": "tenant-a",
        "repo_fingerprint": "github.com/acme/foo",
        "observations": [
            {
                "observation": o.model_dump(mode="json"),
                "signature": sign_observation(o, INGEST_SECRET, "tenant-a", "github.com/acme/foo"),
            }
            for o in obs
        ],
    }
    resp = client.post("/v1/kb/observations", json=body, headers=auth_headers())
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["accepted"] == 2
    assert result["generation"] == 1
    assert result["new_snapshot"] is True

    gen = client.get(
        "/v1/kb/generation",
        params={"tenant_id": "tenant-a", "repo": "github.com/acme/foo"},
        headers=auth_headers(),
    ).json()
    assert gen["generation"] == 1
    assert gen["input_count"] == 2

    snap = client.get(
        "/v1/kb/snapshot",
        params={"tenant_id": "tenant-a", "repo": "github.com/acme/foo"},
        headers=auth_headers(),
    )
    assert snap.status_code == 200
    data = snap.json()
    assert data["generation"] == 1
    assert len(data["snapshot"]["observations"]) == 2

    claw = client.get(
        "/v1/kb/snapshot",
        params={"tenant_id": "tenant-a", "repo": "github.com/acme/foo", "format": "clawtune"},
        headers=auth_headers(),
    )
    assert claw.status_code == 200
    claw_data = claw.json()["snapshot"]
    assert claw_data["schema"] == "runtime_tool_resource_kb_v1"


def test_post_rejects_bad_signature(client):
    obs = make_obs("api-0003")
    body = {
        "tenant_id": "tenant-a",
        "repo_fingerprint": "github.com/acme/foo",
        "observations": [
            {"observation": obs.model_dump(mode="json"), "signature": "not-a-signature"}
        ],
    }
    resp = client.post("/v1/kb/observations", json=body, headers=auth_headers())
    assert resp.status_code == 200
    result = resp.json()
    assert result["accepted"] == 0
    assert len(result["rejected"]) == 1
    assert "HMAC" in result["rejected"][0]["reason"]


def test_replay_is_idempotent(client):
    obs = [make_obs("api-0004")]
    body = {
        "tenant_id": "tenant-a",
        "repo_fingerprint": "github.com/acme/foo",
        "observations": [
            {"observation": o.model_dump(mode="json"), "signature": sign_observation(o, INGEST_SECRET, "tenant-a", "github.com/acme/foo")}
            for o in obs
        ],
    }
    first = client.post("/v1/kb/observations", json=body, headers=auth_headers()).json()
    second = client.post("/v1/kb/observations", json=body, headers=auth_headers()).json()
    assert first["generation"] == 1
    assert second["accepted"] == 0
    assert second["duplicates"] == 1
    assert second["generation"] == 1


def test_rollback_endpoint(client):
    obs = [make_obs("api-0005"), make_obs("api-0006")]
    body = {
        "tenant_id": "tenant-a",
        "repo_fingerprint": "github.com/acme/foo",
        "observations": [
            {"observation": o.model_dump(mode="json"), "signature": sign_observation(o, INGEST_SECRET, "tenant-a", "github.com/acme/foo")}
            for o in obs
        ],
    }
    client.post("/v1/kb/observations", json=body, headers=auth_headers())
    resp = client.post(
        "/v1/kb/rollback",
        json={"tenant_id": "tenant-a", "repo_fingerprint": "github.com/acme/foo"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["generation"] == 0
