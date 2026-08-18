"""Managed control-plane SQLAlchemy rows (ADR-010, M1-3).

The managed tables are the authoritative production schema (managed with
Alembic migrations, never `create_all` in production). They are separate from
the legacy `clawbox.common.db` tables; the legacy tables keep their dev-stage
`create_all` until they are migrated too.

Mapping note: the dataclasses in `clawbox.managed.models` are the API
contract; these rows are the persistence shape. Timestamps are stored as
timezone-aware datetimes and converted to ISO strings at the model boundary.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class ManagedBase(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class RunRow(ManagedBase):
    __tablename__ = "managed_runs"
    run_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    project_id: Mapped[str] = mapped_column(String(128), index=True)
    template_ref: Mapped[str] = mapped_column(String(256))
    template_revision: Mapped[int] = mapped_column(Integer)
    input_ref: Mapped[str] = mapped_column(String(512))
    input_sha256: Mapped[str] = mapped_column(String(64))
    deadline_seconds: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(512))
    request_digest: Mapped[str] = mapped_column(String(64))
    problem_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    phase: Mapped[str] = mapped_column(String(32), default="Accepted")
    desired_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_attempt_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    attempt_counter: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_managed_runs_tenant_idemkey"),
    )


class AttemptRow(ManagedBase):
    __tablename__ = "managed_attempts"
    attempt_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(26), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(32), default="PendingDispatch")
    platform_outcome: Mapped[str] = mapped_column(String(32), default="Pending")
    agent_outcome: Mapped[str] = mapped_column(String(32), default="Pending")
    artifact_outcome: Mapped[str] = mapped_column(String(32), default="Pending")
    evaluation_outcome: Mapped[str] = mapped_column(String(32), default="NotRun")
    desired_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result_manifest_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cancel_after_terminal: Mapped[bool] = mapped_column(Boolean, default=False)
    final_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_number", name="uq_managed_attempts_run_num"),
    )


class RunEventRow(ManagedBase):
    __tablename__ = "managed_run_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    run_id: Mapped[str] = mapped_column(String(26), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    observed_generation: Mapped[int] = mapped_column(Integer, default=1)
    last_transition_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_managed_run_events_run_seq"),
    )


class OutboxRow(ManagedBase):
    __tablename__ = "managed_outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(26), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        Index("ix_managed_outbox_pending", "processed_at", "id"),
    )


class AuditEventRow(ManagedBase):
    __tablename__ = "managed_audit_events"
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    actor: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(128))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(256), index=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def make_managed_engine(url: str | None = None):
    value = url or __import__("os").getenv("DATABASE_URL", "sqlite:///./clawbox.db")
    kwargs: dict[str, Any] = {"future": True}
    if value.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return create_engine(value, **kwargs)


def managed_session_factory(url: str | None = None):
    return sessionmaker(make_managed_engine(url), expire_on_commit=False)


def json_dumps(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
