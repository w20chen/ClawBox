"""Managed repository (ADR-002/010, M1-3).

All writes go through this module so idempotency, event append and outbox
append are one transaction. The dataclasses from `clawbox.managed.models` are
the boundary objects; rows are private.

Semantics:
- `create_run` is idempotent per (tenant_id, idempotency_key): same
  request_digest -> returns the existing Run (200 replay); different
  request_digest -> raises RunConflict (409).
- `new_attempt`/transitions append `managed_run_events` and `managed_outbox`
  rows in the same transaction as the state change.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from clawbox.managed.db import (
    AttemptRow,
    AuditEventRow,
    OutboxRow,
    RunEventRow,
    RunRow,
    from_iso,
    json_dumps,
    to_iso,
)
from clawbox.managed.models import (
    AgentOutcome,
    ArtifactOutcome,
    Attempt,
    AttemptPhase,
    EvaluationOutcome,
    PlatformOutcome,
    Run,
    RunEvent,
    RunIntent,
    RunPhase,
)
from clawbox.managed.state import (
    attempt_transition,
    is_terminal_attempt,
    is_terminal_run,
    run_transition,
)


class RunConflict(Exception):
    """Same idempotency key but a different request body (HTTP 409)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_run(row: RunRow) -> Run:
    return Run(
        run_id=row.run_id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        template_ref=row.template_ref,
        template_revision=row.template_revision,
        input_ref=row.input_ref,
        input_sha256=row.input_sha256,
        deadline_seconds=row.deadline_seconds,
        idempotency_key=row.idempotency_key,
        request_digest=row.request_digest,
        problem_statement=row.problem_statement,
        experiment_spec=__import__("json").loads(row.experiment_spec or "{}"),
        phase=RunPhase(row.phase),
        desired_state=row.desired_state,
        current_attempt_id=row.current_attempt_id,
        attempt_counter=row.attempt_counter,
        created_at=to_iso(row.created_at),
        updated_at=to_iso(row.updated_at),
        committed_at=to_iso(row.committed_at),
        cancel_requested_at=to_iso(row.cancel_requested_at),
        final_reason=row.final_reason,
    )


def _row_to_attempt(row: AttemptRow) -> Attempt:
    return Attempt(
        attempt_id=row.attempt_id,
        run_id=row.run_id,
        attempt_number=row.attempt_number,
        phase=AttemptPhase(row.phase),
        platform_outcome=PlatformOutcome(row.platform_outcome),
        agent_outcome=AgentOutcome(row.agent_outcome),
        artifact_outcome=ArtifactOutcome(row.artifact_outcome),
        evaluation_outcome=EvaluationOutcome(row.evaluation_outcome),
        desired_state=row.desired_state,
        cancel_requested_at=to_iso(row.cancel_requested_at),
        started_at=to_iso(row.started_at),
        finished_at=to_iso(row.finished_at),
        created_at=to_iso(row.created_at),
        updated_at=to_iso(row.updated_at),
        result_manifest_ref=row.result_manifest_ref,
        cancel_after_terminal=row.cancel_after_terminal,
        final_reason=row.final_reason,
    )


def _apply_run_row(row: RunRow, run: Run) -> None:
    row.phase = run.phase.value
    row.desired_state = run.desired_state
    row.current_attempt_id = run.current_attempt_id
    row.attempt_counter = run.attempt_counter
    row.committed_at = from_iso(run.committed_at)
    row.cancel_requested_at = from_iso(run.cancel_requested_at)
    row.final_reason = run.final_reason
    row.updated_at = from_iso(run.updated_at) or _now()


def _apply_attempt_row(row: AttemptRow, attempt: Attempt) -> None:
    row.phase = attempt.phase.value
    row.platform_outcome = attempt.platform_outcome.value
    row.agent_outcome = attempt.agent_outcome.value
    row.artifact_outcome = attempt.artifact_outcome.value
    row.evaluation_outcome = attempt.evaluation_outcome.value
    row.desired_state = attempt.desired_state
    row.cancel_requested_at = from_iso(attempt.cancel_requested_at)
    row.started_at = from_iso(attempt.started_at)
    row.finished_at = from_iso(attempt.finished_at)
    row.result_manifest_ref = attempt.result_manifest_ref
    row.cancel_after_terminal = attempt.cancel_after_terminal
    row.final_reason = attempt.final_reason
    row.updated_at = from_iso(attempt.updated_at) or _now()


def _append_event(
    db: Session, *, run: Run, event_type: str, attempt_id: str | None,
    reason: str | None, message: str | None, observed_generation: int,
    payload: dict[str, Any] | None = None,
) -> None:
    # sequence is the per-run monotonic event number.
    max_seq = db.scalar(
        select(RunEventRow.sequence)
        .where(RunEventRow.run_id == run.run_id)
        .order_by(RunEventRow.sequence.desc())
        .limit(1)
    )
    db.add(RunEventRow(
        sequence=(max_seq or 0) + 1,
        run_id=run.run_id,
        attempt_id=attempt_id,
        event_type=event_type,
        phase=run.phase.value,
        reason=reason,
        message=message,
        observed_generation=observed_generation,
        last_transition_time=_now(),
        payload=json_dumps(payload or {}),
    ))


def _append_outbox(db: Session, *, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
    db.add(OutboxRow(
        run_id=run_id,
        event_type=event_type,
        payload=json_dumps(payload),
        created_at=_now(),
    ))


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def create_run(db: Session, intent: RunIntent) -> tuple[Run, bool]:
    """Idempotent run creation (ADR-002).

    Returns (Run, created). On a replay with the same request digest the
    existing Run is returned; a different digest raises RunConflict.
    """
    now = _now()
    run = Run.new(intent, to_iso(now))
    row = RunRow(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        template_ref=run.template_ref,
        template_revision=run.template_revision,
        input_ref=run.input_ref,
        input_sha256=run.input_sha256,
        deadline_seconds=run.deadline_seconds,
        idempotency_key=run.idempotency_key,
        request_digest=run.request_digest,
        problem_statement=intent.problem_statement,
        experiment_spec=json_dumps(intent.experiment_spec),
        phase=run.phase.value,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    try:
        db.flush()
    except Exception:
        db.rollback()
        existing = db.scalar(
            select(RunRow).where(
                RunRow.tenant_id == intent.tenant_id,
                RunRow.idempotency_key == intent.idempotency_key,
            )
        )
        if existing is None:
            raise
        if existing.request_digest != intent.request_digest:
            raise RunConflict(
                f"idempotency key {intent.idempotency_key!r} already used with a different request body"
            )
        return _row_to_run(existing), False
    _append_event(
        db, run=run, event_type="run.accepted", attempt_id=None,
        reason=None, message="Run accepted",
        observed_generation=1, payload={"idempotencyKey": intent.idempotency_key},
    )
    _append_outbox(db, run_id=run.run_id, event_type="run.accepted", payload={"runId": run.run_id})
    return run, True


def get_run(db: Session, tenant_id: str, run_id: str) -> Run | None:
    row = db.scalar(select(RunRow).where(RunRow.tenant_id == tenant_id, RunRow.run_id == run_id))
    return _row_to_run(row) if row else None


def get_run_by_idempotency(db: Session, tenant_id: str, idempotency_key: str) -> Run | None:
    row = db.scalar(
        select(RunRow).where(
            RunRow.tenant_id == tenant_id, RunRow.idempotency_key == idempotency_key,
        )
    )
    return _row_to_run(row) if row else None


def request_cancel(db: Session, run: Run) -> Run:
    """One-way cancel desired state (ADR-002); idempotent."""
    now = _now()
    if not is_terminal_run(run.phase):
        run.desired_state = "Cancelled"
        run.cancel_requested_at = to_iso(now)
    run.updated_at = to_iso(now)
    row = db.get(RunRow, run.run_id)
    _apply_run_row(row, run)
    _append_event(
        db, run=run, event_type="run.cancel_requested", attempt_id=run.current_attempt_id,
        reason=None, message="Cancel requested" if run.desired_state else "Cancel after terminal",
        observed_generation=1,
    )
    _append_outbox(db, run_id=run.run_id, event_type="run.cancel_requested", payload={"runId": run.run_id})
    return run


def transition_run(db: Session, run: Run, target_phase, *, reason: str | None, message: str | None) -> Run:
    run.phase = run_transition(run.phase, target_phase)
    now = _now()
    if is_terminal_run(run.phase):
        run.committed_at = to_iso(now)
    run.updated_at = to_iso(now)
    row = db.get(RunRow, run.run_id)
    _apply_run_row(row, run)
    _append_event(
        db, run=run, event_type=f"run.{run.phase.value.lower()}", attempt_id=run.current_attempt_id,
        reason=reason, message=message, observed_generation=1,
    )
    _append_outbox(db, run_id=run.run_id, event_type=f"run.{run.phase.value.lower()}", payload={"runId": run.run_id})
    return run


# ---------------------------------------------------------------------------
# Attempts
# ---------------------------------------------------------------------------

def get_attempt(db: Session, attempt_id: str) -> Attempt | None:
    row = db.get(AttemptRow, attempt_id)
    return _row_to_attempt(row) if row else None


def new_attempt(db: Session, run: Run, *, reason: str | None = None) -> Attempt:
    """Create the next attempt for a Run (retry semantics, ADR-002)."""
    now = _now()
    attempt = run.new_attempt(to_iso(now))
    db.add(AttemptRow(
        attempt_id=attempt.attempt_id,
        run_id=attempt.run_id,
        attempt_number=attempt.attempt_number,
        phase=attempt.phase.value,
        platform_outcome=attempt.platform_outcome.value,
        agent_outcome=attempt.agent_outcome.value,
        artifact_outcome=attempt.artifact_outcome.value,
        evaluation_outcome=attempt.evaluation_outcome.value,
        created_at=now,
        updated_at=now,
    ))
    row = db.get(RunRow, run.run_id)
    _apply_run_row(row, run)
    _append_event(
        db, run=run, event_type="attempt.created", attempt_id=attempt.attempt_id,
        reason=reason, message=f"Attempt #{attempt.attempt_number} created",
        observed_generation=1, payload={"attemptId": attempt.attempt_id, "attemptNumber": attempt.attempt_number},
    )
    _append_outbox(
        db, run_id=run.run_id, event_type="attempt.created",
        payload={"runId": run.run_id, "attemptId": attempt.attempt_id, "attemptNumber": attempt.attempt_number},
    )
    return attempt


def transition_attempt(
    db: Session, attempt: Attempt, target_phase, *, reason: str | None = None, message: str | None = None,
) -> Attempt:
    attempt.phase = attempt_transition(attempt.phase, target_phase)
    now = _now()
    if attempt.phase.value == "RuntimeRunning" and attempt.started_at is None:
        attempt.started_at = to_iso(now)
    if is_terminal_attempt(attempt.phase):
        attempt.finished_at = to_iso(now)
    attempt.updated_at = to_iso(now)
    row = db.get(AttemptRow, attempt.attempt_id)
    _apply_attempt_row(row, attempt)
    run = _run_for(db, attempt.run_id)
    _append_event(
        db, run=run, event_type=f"attempt.{attempt.phase.value.lower()}", attempt_id=attempt.attempt_id,
        reason=reason, message=message, observed_generation=1,
    )
    _append_outbox(
        db, run_id=attempt.run_id, event_type=f"attempt.{attempt.phase.value.lower()}",
        payload={"attemptId": attempt.attempt_id, "phase": attempt.phase.value},
    )
    return attempt


def _run_for(db: Session, run_id: str) -> Run:
    row = db.get(RunRow, run_id)
    if row is None:
        raise RuntimeError(f"run {run_id} not found")
    return _row_to_run(row)


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------

def claim_outbox(db: Session, *, limit: int = 10) -> list[OutboxRow]:
    """Claim pending outbox rows for the dispatcher (idempotent workers)."""
    rows = db.scalars(
        select(OutboxRow)
        .where(OutboxRow.processed_at.is_(None))
        .order_by(OutboxRow.id)
        .limit(limit)
    ).all()
    for row in rows:
        row.attempts += 1
    return list(rows)


def complete_outbox(db: Session, outbox_id: int) -> None:
    db.execute(
        update(OutboxRow)
        .where(OutboxRow.id == outbox_id)
        .values(processed_at=_now())
    )


def requeue_outbox(db: Session, outbox_id: int, *, reason: str) -> None:
    db.execute(
        update(OutboxRow)
        .where(OutboxRow.id == outbox_id)
        .values(processed_at=None)
    )


# ---------------------------------------------------------------------------
# Audit (ADR-010)
# ---------------------------------------------------------------------------

def append_audit(
    db: Session, *, tenant_id: str, actor: str, action: str,
    resource_type: str, resource_id: str, payload: dict[str, Any],
) -> None:
    # Monotonic, gap-free sequence (SQLite has no BIGSERIAL autoincrement).
    max_seq = db.scalar(select(AuditEventRow.sequence).order_by(AuditEventRow.sequence.desc()).limit(1))
    db.add(AuditEventRow(
        sequence=(max_seq or 0) + 1,
        tenant_id=tenant_id,
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        payload=json_dumps(payload),
        created_at=_now(),
    ))


def load_run_events(db: Session, run_id: str) -> list[RunEvent]:
    rows = db.scalars(
        select(RunEventRow)
        .where(RunEventRow.run_id == run_id)
        .order_by(RunEventRow.sequence)
    ).all()
    return [
        RunEvent(
            sequence=row.sequence,
            run_id=row.run_id,
            attempt_id=row.attempt_id,
            event_type=row.event_type,
            phase=row.phase,
            reason=row.reason,
            message=row.message,
            observed_generation=row.observed_generation,
            last_transition_time=to_iso(row.last_transition_time),
        )
        for row in rows
    ]
