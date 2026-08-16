from __future__ import annotations

import base64
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from clawbox.common.config import settings
from clawbox.common.auth import require_service_token
from clawbox.common.db import SessionLocal, TaskResultRow, TraceChunkRow, init_db
from clawbox.common.models import StrictModel
from clawbox.ingester.auth import verify_upload_token


class TraceChunk(StrictModel):
    chunk_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    relative_path: str = Field(min_length=1, max_length=512)
    offset: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    data_base64: str = Field(max_length=1_000_000)
    final: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskResult(StrictModel):
    status: str = Field(pattern=r"^(succeeded|failed|timed-out)$")
    final_answer: str = Field(default="", max_length=8_000_000)
    patch: str = Field(default="", max_length=32_000_000)
    logs: dict[str, str] = Field(default_factory=dict)
    session_id: str = Field(default="", max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


def authorize(task_id: str, authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer upload token is required")
    try:
        verify_upload_token(authorization[7:], task_id, settings.ingest_secret)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="ClawBox Trace/Result Ingester", lifespan=lifespan)


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/tasks/{task_id}/traces")
def ingest_trace(task_id: str, chunk: TraceChunk, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    authorize(task_id, authorization)
    try:
        decoded = base64.b64decode(chunk.data_base64, validate=True)
    except ValueError as exc:
        raise HTTPException(422, "data_base64 is invalid") from exc
    if hashlib.sha256(decoded).hexdigest() != chunk.sha256:
        raise HTTPException(422, "trace chunk checksum mismatch")
    if ".." in chunk.relative_path.split("/") or chunk.relative_path.startswith("/"):
        raise HTTPException(422, "relative_path must stay under the task trace root")
    try:
        with SessionLocal.begin() as db:
            existing = db.get(TraceChunkRow, chunk.chunk_id)
            if existing:
                if (
                    existing.task_id != task_id
                    or existing.relative_path != chunk.relative_path
                    or existing.offset != chunk.offset
                    or existing.sha256 != chunk.sha256
                    or existing.final != chunk.final
                ):
                    raise HTTPException(409, "chunk_id collision")
            else:
                occupied = db.scalar(select(TraceChunkRow).where(
                    TraceChunkRow.task_id == task_id,
                    TraceChunkRow.relative_path == chunk.relative_path,
                    TraceChunkRow.offset == chunk.offset,
                    TraceChunkRow.final.is_(chunk.final),
                ))
                if occupied:
                    raise HTTPException(409, "trace offset already contains different content")
                db.add(TraceChunkRow(
                    chunk_id=chunk.chunk_id, task_id=task_id,
                    relative_path=chunk.relative_path, offset=chunk.offset,
                    sha256=chunk.sha256, payload_base64=chunk.data_base64,
                    final=chunk.final, created_at=datetime.now(timezone.utc),
                ))
    except IntegrityError as exc:
        # A concurrent retry may pass the initial read before the first
        # transaction commits. Resolve the unique-key race deterministically.
        with SessionLocal() as db:
            existing = db.get(TraceChunkRow, chunk.chunk_id)
            if existing and (
                existing.task_id == task_id
                and existing.relative_path == chunk.relative_path
                and existing.offset == chunk.offset
                and existing.sha256 == chunk.sha256
                and existing.final == chunk.final
            ):
                return {"accepted": True, "chunk_id": chunk.chunk_id}
        raise HTTPException(409, "trace offset already contains different content") from exc
    return {"accepted": True, "chunk_id": chunk.chunk_id}


@app.post("/v1/tasks/{task_id}/result")
def ingest_result(task_id: str, result: TaskResult, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    authorize(task_id, authorization)
    payload = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    try:
        with SessionLocal.begin() as db:
            existing = db.get(TaskResultRow, task_id)
            if existing and existing.payload_sha256 != digest:
                raise HTTPException(409, "a different immutable result already exists")
            if not existing:
                db.add(TaskResultRow(
                    task_id=task_id, payload_sha256=digest, payload=payload,
                    created_at=datetime.now(timezone.utc),
                ))
    except IntegrityError as exc:
        with SessionLocal() as db:
            existing = db.get(TaskResultRow, task_id)
            if existing and existing.payload_sha256 == digest:
                return {"accepted": True, "sha256": digest}
        raise HTTPException(409, "a different immutable result already exists") from exc
    return {"accepted": True, "sha256": digest}


@app.get("/v1/tasks/{task_id}/receipt")
def receipt(task_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    authorize(task_id, authorization)
    with SessionLocal() as db:
        result = db.get(TaskResultRow, task_id)
        chunks = db.scalar(select(func.count()).select_from(TraceChunkRow).where(TraceChunkRow.task_id == task_id)) or 0
        final = db.scalar(select(func.count()).select_from(TraceChunkRow).where(
            TraceChunkRow.task_id == task_id, TraceChunkRow.final.is_(True)
        )) or 0
    return {"task_id": task_id, "result": bool(result), "trace_chunks": chunks, "trace_final": bool(final),
            "complete": bool(result and final)}


@app.get("/v1/archive/{task_id}/result", dependencies=[Depends(require_service_token)])
def archived_result(task_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        result = db.get(TaskResultRow, task_id)
        if result is None:
            raise HTTPException(404, "task result not found")
        return json.loads(result.payload)


@app.get("/v1/archive/{task_id}/traces", dependencies=[Depends(require_service_token)])
def trace_manifest(task_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        rows = db.scalars(select(TraceChunkRow).where(
            TraceChunkRow.task_id == task_id,
            TraceChunkRow.final.is_(False),
        ).order_by(TraceChunkRow.relative_path, TraceChunkRow.offset)).all()
    paths: dict[str, dict[str, int]] = {}
    for row in rows:
        item = paths.setdefault(row.relative_path, {"chunks": 0, "bytes": 0})
        item["chunks"] += 1
        item["bytes"] += len(base64.b64decode(row.payload_base64))
    return {"task_id": task_id, "paths": paths}


@app.get("/v1/archive/{task_id}/traces/{relative_path:path}", dependencies=[Depends(require_service_token)])
def archived_trace(task_id: str, relative_path: str) -> Response:
    if ".." in relative_path.split("/") or relative_path.startswith("/"):
        raise HTTPException(422, "invalid trace path")
    with SessionLocal() as db:
        rows = db.scalars(select(TraceChunkRow).where(
            TraceChunkRow.task_id == task_id,
            TraceChunkRow.relative_path == relative_path,
            TraceChunkRow.final.is_(False),
        ).order_by(TraceChunkRow.offset)).all()
    if not rows:
        raise HTTPException(404, "trace path not found")
    expected = 0
    parts: list[bytes] = []
    for row in rows:
        if row.offset != expected:
            raise HTTPException(409, "trace archive has an offset gap")
        part = base64.b64decode(row.payload_base64)
        parts.append(part)
        expected += len(part)
    return Response(b"".join(parts), media_type="application/octet-stream")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8084)
