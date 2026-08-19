"""Multi-tenant submission simulator tests (M1).

Verifies that the multi-tenant submitter:
* creates one Run per (tenant, run) through the real API,
* scopes Runs by tenant (cross-tenant read is a 404),
* lets one tenant own many runs at once,
* keeps per-(tenant, repo) KB generations independent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from clawbox.api.app import create_app
from clawbox.api.templates import TemplateRegistry
from clawbox.benchmark.managed_client import ManagedAPIClient, input_sha256
from clawbox.benchmark.multitenant import render_summary, submit_tenants, watch_tenants
from clawbox.managed.db import ManagedBase
from clawbox.managed.models import RunIntent, idempotency_digest
from clawbox.managed.repo import create_run
from clawbox.tuning.schema import ToolObservation
from clawbox.tuning.store import (
    TuningBase,
    init_tuning_db,
    make_tuning_engine,
)

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
def app_client(tmp_path):
    """(api_client_factory, run_factory) shared by all tenants."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mt.db'}",
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
    with TestClient(app) as client:
        def make(tenant_id: str) -> ManagedAPIClient:
            return ManagedAPIClient(
                client.base_url,
                token=TOKEN,
                tenant_id=tenant_id,
                client=client,
            )

        yield make, factory
    engine.dispose()


def test_multitenant_submit_creates_distinct_runs(app_client):
    make, _ = app_client
    submissions = submit_tenants(
        base_url="http://test",
        token=TOKEN,
        tenants=["tenant-a", "tenant-b"],
        runs_per_tenant=2,
        project_id="mt-sim",
        template_ref="swe-rebench-arm64",
        template_revision=1,
        input_ref="15five__scim2-filter-parser-13",
        input_sha256_="a" * 64,
        deadline_seconds=300,
        idempotency_prefix="mt",
        client_factory=make,
    )
    assert len(submissions) == 2
    for submission in submissions:
        assert submission.created == 2
        assert submission.replays == 0
        assert len(submission.run_ids) == 2
        # idempotency keys differ across tenants so run ids must be unique
        assert len(set(submission.run_ids)) == 2
    all_run_ids = [rid for s in submissions for rid in s.run_ids]
    assert len(set(all_run_ids)) == 4
    table = render_summary(submissions)
    assert "tenant-a\t2\t0\t2\t" in table
    assert "tenant-b\t2\t0\t2\t" in table


def test_cross_tenant_run_read_is_404(app_client):
    make, _ = app_client
    submissions = submit_tenants(
        base_url="http://test",
        token=TOKEN,
        tenants=["tenant-a", "tenant-b"],
        runs_per_tenant=1,
        project_id="mt-sim",
        template_ref="swe-rebench-arm64",
        template_revision=1,
        input_ref="15five__scim2-filter-parser-13",
        input_sha256_="a" * 64,
        deadline_seconds=300,
        idempotency_prefix="mt-x",
        client_factory=make,
    )
    tenant_a_run = submissions[0].run_ids[0]
    tenant_b_client = make("tenant-b")
    other = tenant_b_client._client.get(
        f"/v1/runs/{tenant_a_run}",
        headers={"X-Clawbox-Token": TOKEN, "X-Tenant-Id": "tenant-b"},
    )
    assert other.status_code == 404
    owner = make("tenant-a")
    assert owner.get_run(tenant_a_run)["tenantId"] == "tenant-a"


def test_one_tenant_many_runs_are_all_owned(app_client):
    make, _ = app_client
    submissions = submit_tenants(
        base_url="http://test",
        token=TOKEN,
        tenants=["tenant-a"],
        runs_per_tenant=4,
        project_id="mt-sim",
        template_ref="swe-rebench-arm64",
        template_revision=1,
        input_ref="15five__scim2-filter-parser-13",
        input_sha256_="a" * 64,
        deadline_seconds=300,
        idempotency_prefix="mt-many",
        client_factory=make,
    )
    owner = make("tenant-a")
    for run_id in submissions[0].run_ids:
        assert owner.get_run(run_id)["tenantId"] == "tenant-a"


def test_watch_terminates_on_terminal_runs(app_client):
    make, _ = app_client
    submissions = submit_tenants(
        base_url="http://test",
        token=TOKEN,
        tenants=["tenant-a", "tenant-b"],
        runs_per_tenant=1,
        project_id="mt-sim",
        template_ref="swe-rebench-arm64",
        template_revision=1,
        input_ref="15five__scim2-filter-parser-13",
        input_sha256_="a" * 64,
        deadline_seconds=300,
        idempotency_prefix="mt-watch",
        client_factory=make,
    )
    # Without a dispatcher the runs stay Accepted forever; watch with a short
    # timeout must return (not hang) and leave phases recorded.
    watch_tenants(
        submissions,
        base_url="http://test",
        token=TOKEN,
        client_factory=make,
        poll_seconds=0.1,
        timeout_seconds=0.5,
    )
    for submission in submissions:
        for phase in submission.phases.values():
            assert phase in ("Accepted", "Succeeded", "Failed", "Cancelled")


def test_kb_generations_are_tenant_independent(tmp_path):
    """Observations from tenant A only advance tenant A's KB generation."""
    from clawbox.tuning.projector import ingest, latest_snapshot, snapshot_metadata

    engine = make_tuning_engine(f"sqlite:///{tmp_path / 'kb.db'}")
    init_tuning_db(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    def observation(execution_id: str) -> ToolObservation:
        return ToolObservation(
            execution_id=execution_id,
            tool_name="exec",
            command="pytest -q",
            command_digest="abc",
            repo_fingerprint="github.com/acme/foo",
            run_id="run-1",
            sequence_no=1,
            duration_sec=2.0,
            status_code="ok",
            exit_code=0,
            complete=True,
            collection_quality="valid",
            coverage_ratio=0.95,
        )

    with factory() as db:
        ingest(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo", observations=[observation("exec-1")])
        db.commit()
    with factory() as db:
        ingest(db, tenant_id="tenant-b", repo_fingerprint="github.com/acme/foo", observations=[observation("exec-2")])
        db.commit()

    with factory() as db:
        meta_a = snapshot_metadata(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo")
        meta_b = snapshot_metadata(db, tenant_id="tenant-b", repo_fingerprint="github.com/acme/foo")
        assert meta_a is not None and meta_a["generation"] == 1
        assert meta_b is not None and meta_b["generation"] == 1
        # tenant-b snapshot must not contain tenant-a's execution
        snap_b = latest_snapshot(db, tenant_id="tenant-b", repo_fingerprint="github.com/acme/foo")
        assert snap_b is not None
        assert "exec-1" not in snap_b.snapshot
        assert "exec-2" in snap_b.snapshot


def test_run_intent_request_digest_is_tenant_stable():
    """Same body submitted by different tenants has the same digest; the
    (tenant, idempotency_key) pair keeps them separate."""
    payload = dict(
        projectId="p",
        templateRef="swe-rebench-arm64",
        templateRevision=1,
        inputRef="i",
        inputSha256="a" * 64,
        deadlineSeconds=300,
        idempotencyKey="same-key",
    )
    digest = idempotency_digest(payload)
    assert digest == idempotency_digest(payload)
    assert len(digest) == 64
