"""Control-plane persistence for trusted observations and KB snapshots (P1).

The tuning pipeline is offline-only until this store exists: `clawbox/tuning/`
builds datasets and KB snapshots from raw traces, but nothing persisted the
trusted observation set or the per-(tenant, repo) KB generations.  This module
adds the append-only trusted store and the immutable snapshot history the
handoff (ADR-008 §4, P1) calls for:

* ``tuning_observations`` — append-only trusted observations, deduplicated by
  ``(tenant_id, repo_fingerprint, execution_id, tool_name, sequence_no)``.
  Replaying a signed batch is idempotent: a repeat key is dropped, never
  re-trained.
* ``tuning_kb_snapshots`` — immutable per-``(tenant_id, repo_fingerprint)``
  generations.  Each snapshot stores both the research format
  (``KnowledgeBase.snapshot()``) and the ClawTune-loadable format
  (``RuntimeToolResourceKB.to_json_obj`` shape, see ``tuning/clawtune.py``).

SQLite-first (the paper path); the same schema is PostgreSQL-compatible.
Timestamps are stored timezone-aware; JSON payloads use the canonical
compact encoding shared with the managed tables.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    ForeignKey,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .schema import ToolObservation


class TuningBase(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class TuningObservationRow(TuningBase):
    __tablename__ = "tuning_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    repo_fingerprint: Mapped[str] = mapped_column(String(256), index=True)
    execution_id: Mapped[str] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(128))
    sequence_no: Mapped[int] = mapped_column(Integer)
    # Canonical ToolObservation JSON (model_dump(mode="json")).
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "repo_fingerprint",
            "execution_id",
            "tool_name",
            "sequence_no",
            name="uq_tuning_obs_tenant_repo_exec_tool_seq",
        ),
        Index("ix_tuning_obs_tenant_repo_created", "tenant_id", "repo_fingerprint", "created_at"),
    )


class TuningKBSnapshotRow(TuningBase):
    __tablename__ = "tuning_kb_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    repo_fingerprint: Mapped[str] = mapped_column(String(256), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    # Research-format snapshot (KnowledgeBase.snapshot()).
    snapshot: Mapped[str] = mapped_column(Text)
    # ClawTune-loadable snapshot (RuntimeToolResourceKB.to_json_obj shape).
    clawtune_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_digest: Mapped[str] = mapped_column(String(64))
    input_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "repo_fingerprint",
            "generation",
            name="uq_tuning_kb_snapshots_tenant_repo_gen",
        ),
        Index("ix_tuning_kb_snapshots_tenant_repo_gen", "tenant_id", "repo_fingerprint", "generation"),
    )


class TuningNativeBatchRow(TuningBase):
    """Immutable signed manifest, including rejected batches for audit."""

    __tablename__ = "tuning_native_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    repo_fingerprint: Mapped[str] = mapped_column(String(256), index=True)
    manifest_digest: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(128))
    attempt_id: Mapped[str] = mapped_column(String(128))
    cell_id: Mapped[str] = mapped_column(String(128))
    clawtune_revision: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32))
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "repo_fingerprint",
            "manifest_digest",
            name="uq_tuning_native_batch_tenant_repo_digest",
        ),
        Index(
            "ix_tuning_native_batch_tenant_repo_created",
            "tenant_id",
            "repo_fingerprint",
            "created_at",
        ),
    )


class TuningNativeArtifactRow(TuningBase):
    """Raw byte-preserving artifact stored before native projection."""

    __tablename__ = "tuning_native_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("tuning_native_batches.id", ondelete="RESTRICT"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    repo_fingerprint: Mapped[str] = mapped_column(String(256), index=True)
    execution_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    artifact_digest: Mapped[str] = mapped_column(String(64))
    content_b64: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "repo_fingerprint",
            "artifact_digest",
            name="uq_tuning_native_artifact_tenant_repo_digest",
        ),
    )


class TuningNativeSnapshotRow(TuningBase):
    """Atomic pair of native ClawTune snapshots for one generation."""

    __tablename__ = "tuning_native_kb_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    repo_fingerprint: Mapped[str] = mapped_column(String(256), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    clause_snapshot: Mapped[str] = mapped_column(Text)
    runtime_snapshot: Mapped[str] = mapped_column(Text)
    pair_digest: Mapped[str] = mapped_column(String(64))
    source_digest: Mapped[str] = mapped_column(String(64))
    artifact_count: Mapped[int] = mapped_column(Integer)
    evidence: Mapped[str] = mapped_column(Text)
    clawtune_revision: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "repo_fingerprint",
            "generation",
            name="uq_tuning_native_snapshot_tenant_repo_gen",
        ),
        Index(
            "ix_tuning_native_snapshot_tenant_repo_gen",
            "tenant_id",
            "repo_fingerprint",
            "generation",
        ),
    )


def _make_sqlite_immediate(engine) -> None:
    """Serialize SQLite write transactions (BEGIN IMMEDIATE).

    The projector's ingest does read-then-write (existing dedup keys + latest
    generation) and must see fresh state: with the driver's default deferred
    BEGIN, two concurrent writers can both read the old generation and then
    collide on the snapshot unique constraint, losing observations.  With
    BEGIN IMMEDIATE the second writer blocks at the transaction start until
    the first commits, so its reads observe the committed state.
    """

    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, _connection_record) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def make_tuning_engine(url: str | None = None):
    value = url or os.getenv("TUNING_DATABASE_URL") or os.getenv("DATABASE_URL", "sqlite:///./clawbox.db")
    kwargs: dict[str, Any] = {"future": True}
    if value.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(value, **kwargs)
    if value.startswith("sqlite"):
        _make_sqlite_immediate(engine)
    return engine


def tuning_session_factory(url: str | None = None):
    return sessionmaker(make_tuning_engine(url), expire_on_commit=False)


def init_tuning_db(engine) -> None:
    """Create the tuning tables (dev/research path; production uses Alembic)."""
    TuningBase.metadata.create_all(bind=engine)


# ── payload (de)serialization ──────────────────────────────────────────

def observation_to_payload(observation: ToolObservation) -> str:
    return json_dumps(observation.model_dump(mode="json"))


def payload_to_observation(payload: str) -> ToolObservation:
    return ToolObservation.model_validate(json.loads(payload))


def row_to_observation(row: TuningObservationRow) -> ToolObservation:
    return payload_to_observation(row.payload)


def dedup_key_from_row(row: TuningObservationRow) -> tuple[str, str, int]:
    return (row.execution_id, row.tool_name, row.sequence_no)


def observation_dedup_key(observation: ToolObservation) -> tuple[str, str, int]:
    return (observation.execution_id, observation.tool_name, observation.sequence_no)
