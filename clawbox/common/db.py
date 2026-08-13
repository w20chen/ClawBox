from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


class TenantRow(Base):
    __tablename__ = "tenants"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    cpu_quota: Mapped[int] = mapped_column(Integer, default=32)
    concurrency_quota: Mapped[int] = mapped_column(Integer, default=4)


class ExecutionRow(Base):
    __tablename__ = "executions"
    execution_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    workspace_id: Mapped[str] = mapped_column(String(128))
    command_digest: Mapped[str] = mapped_column(String(64))
    intent_payload: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="PREDICTED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LeaseRow(Base):
    __tablename__ = "resource_leases"
    lease_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    execution_id: Mapped[str] = mapped_column(String(128), unique=True)
    cpu_count: Mapped[int] = mapped_column(Integer)
    memory_bytes: Mapped[int] = mapped_column(BigInteger)
    numa_hint: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allocator_epoch: Mapped[str] = mapped_column(String(64))
    fencing_token: Mapped[int] = mapped_column(BigInteger, unique=True, autoincrement=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ObservationRow(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(128))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    observation_type: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)
    trusted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("execution_id", "observation_type", "version"),)


class KBMetadataRow(Base):
    __tablename__ = "kb_metadata"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkspaceBindingRow(Base):
    __tablename__ = "workspace_bindings"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    tool_pod_uid: Mapped[str] = mapped_column(String(128), unique=True)
    endpoint: Mapped[str] = mapped_column(String(512))


class ToolInstanceRow(Base):
    __tablename__ = "tool_instances"
    tool_pod_uid: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    backend: Mapped[str] = mapped_column(String(32))
    backend_id: Mapped[str] = mapped_column(String(256))
    endpoint: Mapped[str] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(32))


def make_engine(url: str | None = None):
    value = url or os.getenv("DATABASE_URL", settings.database_url)
    kwargs = {"connect_args": {"check_same_thread": False}} if value.startswith("sqlite") else {}
    return create_engine(value, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    # Scheduler, Allocator and Controller may all start immediately after
    # PostgreSQL becomes healthy. PostgreSQL's CREATE TABLE IF NOT EXISTS is
    # not race-free at the catalog level when independent processes create
    # the same type/table concurrently. Serialize only this short bootstrap
    # transaction; normal service traffic does not take this lock.
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(1129071200)"))
        Base.metadata.create_all(bind=connection)
