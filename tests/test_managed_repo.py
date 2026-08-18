"""M1-3 repository tests: idempotency, retry, outbox/event append (ADR-002/010)."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from clawbox.managed.db import (
    ManagedBase,
    OutboxRow,
    RunEventRow,
    RunRow,
)
from clawbox.managed.models import AttemptPhase, RunIntent, RunPhase, idempotency_digest
from clawbox.managed.repo import (
    RunConflict,
    append_audit,
    claim_outbox,
    complete_outbox,
    create_run,
    get_attempt,
    get_run,
    load_run_events,
    new_attempt,
    request_cancel,
    transition_attempt,
    transition_run,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", future=True)
    ManagedBase.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()
    engine.dispose()


def make_intent(**overrides) -> RunIntent:
    base = dict(
        tenant_id="tenant-a",
        project_id="proj-1",
        template_ref="swe-rebench-arm64",
        template_revision=3,
        input_ref="15five__scim2-filter-parser-13",
        input_sha256="a" * 64,
        deadline_seconds=1800,
        idempotency_key="key-1",
        request_digest=idempotency_digest({"tool": "swe-rebench-arm64", "task": "scim2-13"}),
    )
    base.update(overrides)
    return RunIntent(**base)


def test_create_run_and_idempotent_replay(db):
    run1, created1 = create_run(db, make_intent())
    db.commit()
    assert created1 is True
    assert run1.phase == RunPhase.ACCEPTED

    # Replay with the same key + body returns the same Run.
    run2, created2 = create_run(db, make_intent())
    db.commit()
    assert created2 is False
    assert run2.run_id == run1.run_id

    # Exactly one row.
    rows = db.scalars(select(RunRow)).all()
    assert len(rows) == 1


def test_create_run_conflict_on_different_body(db):
    create_run(db, make_intent())
    db.commit()
    with pytest.raises(RunConflict):
        create_run(db, make_intent(request_digest=idempotency_digest({"tool": "other"})))
    db.rollback()


def test_create_run_accepts_second_tenant_same_key(db):
    run1, _ = create_run(db, make_intent())
    run2, created2 = create_run(db, make_intent(tenant_id="tenant-b"))
    db.commit()
    assert created2 is True
    assert run1.run_id != run2.run_id


def test_retry_creates_new_attempt_rows(db):
    run, _ = create_run(db, make_intent())
    first = new_attempt(db, run)
    second = new_attempt(db, run)
    db.commit()
    assert first.attempt_id != second.attempt_id
    assert first.attempt_number == 1
    assert second.attempt_number == 2
    loaded = get_attempt(db, second.attempt_id)
    assert loaded is not None and loaded.attempt_number == 2
    # Each attempt has its own event/outbox rows.
    events = load_run_events(db, run.run_id)
    assert len(events) == 3  # run.accepted + attempt.created x2


def test_transition_run_appends_event_and_outbox(db):
    run, _ = create_run(db, make_intent())
    transition_run(db, run, RunPhase.QUEUED, reason=None, message="dispatched")
    transition_run(db, run, RunPhase.RUNNING, reason=None, message="admitted")
    transition_run(db, run, RunPhase.FINALIZING, reason=None, message="collecting")
    transition_run(db, run, RunPhase.SUCCEEDED, reason=None, message="done")
    db.commit()
    assert run.phase == RunPhase.SUCCEEDED
    assert run.committed_at is not None
    events = load_run_events(db, run.run_id)
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)
    assert sequences == list(range(1, len(events) + 1))
    outbox = db.scalars(select(OutboxRow).order_by(OutboxRow.id)).all()
    assert [o.event_type for o in outbox] == [
        "run.accepted", "run.queued", "run.running", "run.finalizing", "run.succeeded",
    ]


def test_transition_attempt_happy_path(db):
    run, _ = create_run(db, make_intent())
    attempt = new_attempt(db, run)
    for target in (
        AttemptPhase.QUEUED,
        AttemptPhase.ADMITTED,
        AttemptPhase.TOOL_STARTING,
        AttemptPhase.TOOL_READY,
        AttemptPhase.RUNTIME_RUNNING,
        AttemptPhase.COLLECTING,
        AttemptPhase.SUCCEEDED,
    ):
        transition_attempt(db, attempt, target)
    db.commit()
    assert attempt.phase == AttemptPhase.SUCCEEDED
    assert attempt.started_at is not None
    assert attempt.finished_at is not None
    assert get_attempt(db, attempt.attempt_id).phase == AttemptPhase.SUCCEEDED


def test_cancel_is_one_way_and_idempotent(db):
    run, _ = create_run(db, make_intent())
    request_cancel(db, run)
    db.commit()
    assert run.desired_state == "Cancelled"
    assert run.cancel_requested_at is not None
    # Idempotent second cancel does not duplicate terminal commit.
    request_cancel(db, run)
    db.commit()
    events = [e for e in load_run_events(db, run.run_id) if e.event_type == "run.cancel_requested"]
    assert len(events) == 2  # both cancel requests recorded as distinct events


def test_outbox_claim_and_complete(db):
    run, _ = create_run(db, make_intent())
    new_attempt(db, run)
    db.commit()
    pending = claim_outbox(db, limit=10)
    assert len(pending) == 2
    assert all(row.processed_at is None for row in pending)
    assert all(row.attempts == 1 for row in pending)
    complete_outbox(db, pending[0].id)
    db.commit()
    remaining = db.scalars(
        select(OutboxRow).where(OutboxRow.processed_at.is_(None))
    ).all()
    assert len(remaining) == 1


def test_audit_append(db):
    append_audit(
        db, tenant_id="tenant-a", actor="user@example.com",
        action="run.create", resource_type="run", resource_id="run-1",
        payload={"via": "api"},
    )
    db.commit()
    from clawbox.managed.db import AuditEventRow

    rows = db.scalars(select(AuditEventRow)).all()
    assert len(rows) == 1
    assert rows[0].action == "run.create"


def test_get_run_scoped_by_tenant(db):
    run, _ = create_run(db, make_intent())
    db.commit()
    assert get_run(db, "tenant-a", run.run_id) is not None
    assert get_run(db, "tenant-b", run.run_id) is None
