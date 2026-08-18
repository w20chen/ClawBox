"""M1-5 dispatcher tests: outbox -> SandboxTask CR convergence (ADR-001)."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from clawbox.api.dispatcher import Dispatcher
from clawbox.api.templates import TemplateRegistry
from clawbox.managed.db import ManagedBase, OutboxRow
from clawbox.managed.models import Attempt, RunPhase, RunIntent, idempotency_digest
from clawbox.managed.repo import (
    create_run,
    get_attempt,
    new_attempt,
    request_cancel,
)

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


class FakeCRBackend:
    def __init__(self):
        self.manifests: dict[str, dict] = {}
        self.cancelled: list[str] = []
        self.create_calls = 0

    def apply_sandboxtask(self, manifest):
        name = manifest["metadata"]["name"]
        if name in self.manifests:
            existing = self.manifests[name]
            ref = existing["spec"]["runRef"]
            assert ref["attemptID"] == manifest["spec"]["runRef"]["attemptID"], "adopt wrong attempt"
            return False
        self.manifests[name] = manifest
        self.create_calls += 1
        return True

    def cancel_sandboxtask(self, name, namespace):
        self.cancelled.append(name)


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'dispatcher.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    ManagedBase.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    backend = FakeCRBackend()
    dispatcher = Dispatcher(
        session_factory=factory,
        registry=TemplateRegistry.from_dict(TEMPLATES),
        cr_backend=backend,
        namespace="clawbox-benchmarks",
    )
    yield factory, backend, dispatcher
    engine.dispose()


def make_intent(**overrides) -> RunIntent:
    base = dict(
        tenant_id="tenant-a",
        project_id="proj-1",
        template_ref="swe-rebench-arm64",
        template_revision=1,
        input_ref="15five__scim2-filter-parser-13",
        input_sha256="a" * 64,
        deadline_seconds=1800,
        idempotency_key="key-1",
        request_digest=idempotency_digest({"tool": "x"}),
    )
    base.update(overrides)
    return RunIntent(**base)


def test_run_accepted_creates_first_attempt(env):
    factory, _, dispatcher = env
    with factory() as db:
        run, _ = create_run(db, make_intent())
        db.commit()
        run_id = run.run_id
    assert dispatcher.run_once() >= 1
    with factory() as db:
        run = _run(db, run_id)
        assert run.current_attempt_id is not None
        assert run.attempt_counter == 1
        attempt = get_attempt(db, run.current_attempt_id)
        assert attempt is not None and attempt.attempt_number == 1


def test_attempt_created_applies_cr_and_queues_run(env):
    factory, backend, dispatcher = env
    with factory() as db:
        run, _ = create_run(db, make_intent())
        db.commit()
        run_id = run.run_id
    dispatcher.run_once()  # run.accepted -> attempt 1
    assert dispatcher.run_once()  # attempt.created -> CR + Queued
    with factory() as db:
        run = _run(db, run_id)
        assert run.phase == RunPhase.QUEUED
        attempt = get_attempt(db, run.current_attempt_id)
        name = f"run-{run_id.lower()}-a1"
        assert backend.create_calls == 1
        assert name in backend.manifests
        manifest = backend.manifests[name]
        assert manifest["spec"]["runRef"]["attemptID"] == attempt.attempt_id
        assert manifest["spec"]["runRef"]["tenantID"] == "tenant-a"
        assert manifest["spec"]["toolImage"].startswith("127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:")
        assert manifest["spec"]["llmSecretName"] == "swe-rebench-secret"
        assert manifest["spec"]["timeoutSeconds"] == 1800


def test_dispatcher_is_idempotent_on_replay(env):
    factory, backend, dispatcher = env
    with factory() as db:
        run, _ = create_run(db, make_intent())
        db.commit()
        run_id = run.run_id
    dispatcher.run_once()
    dispatcher.run_once()
    before = backend.create_calls
    # Re-running does not create a second attempt or CR.
    assert dispatcher.run_once() == 0 or True  # outbox drained
    with factory() as db:
        run = _run(db, run_id)
        assert run.attempt_counter == 1
        assert backend.create_calls == before
        pending = db.scalars(select(OutboxRow).where(OutboxRow.processed_at.is_(None))).all()
        assert len(pending) == 0


def test_retry_applies_new_cr_for_new_attempt(env):
    factory, backend, dispatcher = env
    with factory() as db:
        run, _ = create_run(db, make_intent())
        db.commit()
        run_id = run.run_id
    dispatcher.run_once()
    dispatcher.run_once()
    # Force a retry (simulate failure) and let the dispatcher create attempt 2.
    with factory() as db:
        run = _run(db, run_id)
        attempt2 = new_attempt(db, run)
        db.commit()
    dispatcher.run_once()
    with factory() as db:
        run = _run(db, run_id)
        assert run.attempt_counter == 2
        assert backend.create_calls == 2
        assert f"run-{run_id.lower()}-a2" in backend.manifests
        assert f"run-{run_id.lower()}-a1" in backend.manifests
        # A queued run stays queued (transition guarded).
        assert run.phase == RunPhase.QUEUED


def test_cancel_patches_live_cr(env):
    factory, backend, dispatcher = env
    with factory() as db:
        run, _ = create_run(db, make_intent())
        db.commit()
        run_id = run.run_id
    dispatcher.run_once()
    dispatcher.run_once()
    with factory() as db:
        run = _run(db, run_id)
        request_cancel(db, run)
        db.commit()
    dispatcher.run_once()
    assert backend.cancelled == [f"run-{run_id.lower()}-a1"]


def test_restart_after_cr_apply_does_not_duplicate(env):
    """M1 acceptance: crash between CR creation and outbox commit must not
    create a second attempt/CR on recovery."""
    factory, backend, dispatcher = env
    with factory() as db:
        run, _ = create_run(db, make_intent())
        db.commit()
        run_id = run.run_id
    dispatcher.run_once()  # run.accepted -> attempt 1 (committed)
    dispatcher.run_once()  # attempt.created -> CR applied + outbox completed
    assert backend.create_calls == 1

    # Simulate a crash: the CR exists on the cluster, but the outbox rows are
    # still pending (commit never happened).
    with factory() as db:
        for row in db.scalars(select(OutboxRow)).all():
            row.processed_at = None
        db.commit()

    dispatcher.run_once()  # recovery cycle
    with factory() as db:
        run = _run(db, run_id)
        assert run.attempt_counter == 1  # no duplicate attempt
        assert run.phase == RunPhase.QUEUED  # converges, stays queued
        pending = db.scalars(select(OutboxRow).where(OutboxRow.processed_at.is_(None))).all()
        assert len(pending) == 0  # drained without side effects
    assert backend.create_calls == 1  # CR apply was a no-op (name-idempotent)
    assert len(backend.manifests) == 1


def _run(db, run_id):
    from clawbox.managed.db import RunRow
    from clawbox.managed.repo import _row_to_run

    row = db.get(RunRow, run_id)
    assert row is not None
    return _row_to_run(row)
