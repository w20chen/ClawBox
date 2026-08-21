from __future__ import annotations

import os
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, text

from clawbox.common.config import settings
from clawbox.common.db import LeaseRow, SessionLocal, TenantRow
from clawbox.common.models import LeaseState, ReleaseLease, RenewLease, ResourceLease, ResourceRequest, utcnow


class Allocator:
    def __init__(self) -> None:
        self.epoch = os.getenv("ALLOCATOR_EPOCH", str(uuid.uuid4()))

    def _capacity(self) -> tuple[int, dict[int, int]]:
        raw = os.getenv("NUMA_CAPACITY", "0:64")
        nodes = {int(k): int(v) for k, v in (part.split(":", 1) for part in raw.split(","))}
        return sum(nodes.values()), nodes

    def create(self, request: ResourceRequest) -> ResourceLease:
        now = utcnow()
        with SessionLocal.begin() as db:
            if db.bind and db.bind.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(1129071199)"))
            self._mark_expired(db, now)
            tenant = db.scalar(select(TenantRow).where(
                TenantRow.tenant_id == request.tenant_id
            ).with_for_update())
            if tenant is None:
                tenant = TenantRow(tenant_id=request.tenant_id)
                db.add(tenant)
                db.flush()
            existing = db.scalar(select(LeaseRow).where(LeaseRow.execution_id == request.execution_id))
            if existing is not None:
                if (
                    existing.tenant_id != request.tenant_id
                    or existing.cpu_count != request.cpu_count
                    or existing.memory_bytes != request.memory_bytes
                ):
                    raise HTTPException(409, "execution_id is already bound to a different request")
                return self._model(existing)
            active = list(db.scalars(select(LeaseRow).where(
                LeaseRow.tenant_id == request.tenant_id,
                LeaseRow.state.in_([LeaseState.ACTIVE.value, LeaseState.LEASE_EXPIRED.value]),
            )))
            if sum(row.cpu_count for row in active) + request.cpu_count > tenant.cpu_quota:
                raise HTTPException(409, "tenant CPU quota exceeded")
            if len(active) >= tenant.concurrency_quota:
                raise HTTPException(409, "tenant concurrency quota exceeded")
            total, nodes = self._capacity()
            all_active = list(db.scalars(select(LeaseRow).where(
                LeaseRow.state.in_([LeaseState.ACTIVE.value, LeaseState.LEASE_EXPIRED.value])
            ).with_for_update()))
            used_total = sum(row.cpu_count for row in all_active)
            reserved = min(max(0, int(total * settings.reserved_cpu_fraction + 0.999)), max(0, total - 1))
            if used_total + request.cpu_count > total - reserved:
                raise HTTPException(409, "global CPU capacity exhausted")
            used_by_node = {node: 0 for node in nodes}
            for row in all_active:
                if row.numa_hint in used_by_node:
                    used_by_node[row.numa_hint] += row.cpu_count
            choices = list(nodes)
            if request.preferred_numa in nodes:
                choices.remove(request.preferred_numa)
                choices.insert(0, request.preferred_numa)
            numa = next((node for node in choices if nodes[node] - used_by_node[node] >= request.cpu_count), None)
            if numa is None:
                raise HTTPException(409, "no single NUMA node has sufficient logical capacity")
            max_token = db.scalar(select(func.max(LeaseRow.fencing_token))) or 0
            row = LeaseRow(
                lease_id=str(uuid.uuid4()), tenant_id=request.tenant_id,
                execution_id=request.execution_id, cpu_count=request.cpu_count,
                memory_bytes=request.memory_bytes, numa_hint=numa,
                allocator_epoch=self.epoch, fencing_token=max_token + 1,
                state=LeaseState.ACTIVE.value, created_at=now,
                expires_at=now + timedelta(seconds=settings.lease_ttl_seconds),
            )
            db.add(row)
            db.flush()
            return self._model(row)

    def renew(self, lease_id: str, request: RenewLease) -> ResourceLease:
        with SessionLocal.begin() as db:
            now = utcnow()
            self._mark_expired(db, now)
            row = db.get(LeaseRow, lease_id)
            self._fence(row, request.fencing_token)
            if row.state != LeaseState.ACTIVE.value:
                raise HTTPException(409, f"lease is {row.state}")
            row.expires_at = now + timedelta(seconds=request.ttl_seconds)
            return self._model(row)

    def release(self, lease_id: str, request: ReleaseLease) -> ResourceLease:
        with SessionLocal.begin() as db:
            row = db.get(LeaseRow, lease_id)
            self._fence(row, request.fencing_token)
            if request.workload_stopped:
                row.state = LeaseState.RELEASED.value
            elif row.state == LeaseState.LEASE_EXPIRED.value:
                row.state = LeaseState.RESOURCE_RECLAIMABLE.value
            else:
                raise HTTPException(409, "capacity release requires workload_stopped confirmation")
            return self._model(row)

    def capacity(self) -> dict:
        total, nodes = self._capacity()
        with SessionLocal.begin() as db:
            self._mark_expired(db, utcnow())
            active = list(db.scalars(select(LeaseRow).where(
                LeaseRow.state.in_([LeaseState.ACTIVE.value, LeaseState.LEASE_EXPIRED.value])
            )))
        used = sum(row.cpu_count for row in active)
        reserved = min(max(0, int(total * settings.reserved_cpu_fraction + 0.999)), max(0, total - 1))
        return {"total_cpu": total, "active_cpu": used, "available_cpu": max(0, total - reserved - used),
                "numa_capacity": nodes, "allocator_epoch": self.epoch}

    @staticmethod
    def _mark_expired(db, now) -> None:
        for row in db.scalars(select(LeaseRow).where(
            LeaseRow.state == LeaseState.ACTIVE.value, LeaseRow.expires_at <= now
        )):
            row.state = LeaseState.LEASE_EXPIRED.value

    @staticmethod
    def _fence(row, token: int) -> None:
        if row is None:
            raise HTTPException(404, "lease not found")
        if row.fencing_token != token:
            raise HTTPException(409, "stale fencing token")

    @staticmethod
    def _model(row: LeaseRow) -> ResourceLease:
        return ResourceLease(**{name: getattr(row, name) for name in ResourceLease.model_fields})
