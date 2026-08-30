"""Tuning KB control-plane API (P1) — serves snapshots + ingests observations.

Endpoints (all require ``Authorization: Bearer <service_token>``):

* ``GET  /v1/kb/generation?tenant_id&repo`` — latest generation metadata.
* ``GET  /v1/kb/snapshot?tenant_id&repo&format=research|clawtune`` —
  latest immutable snapshot in the requested format.
* ``POST /v1/kb/observations`` — signed observation batch; returns
  ``{generation, accepted, rejected, duplicates}``.
* ``POST /v1/kb/rollback`` — drop the latest generation (returns new gen).

Persistence is SQLite-first (``TUNING_DATABASE_URL`` or ``DATABASE_URL``);
the tables are created on startup for the research/dev path.
"""

from __future__ import annotations

import os
import json
import math
import time
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from clawbox.common.auth import require_service_token
from clawbox.common.config import settings

from .projector import (
    ingest,
    latest_snapshot,
    rollback,
    snapshot_metadata,
    snapshot_row_to_dict,
)
from .native import NativeTelemetryManifest, _clawtune_api
from .native_projector import (
    ingest_native_batch,
    latest_native_snapshot,
    native_snapshot_for_generation,
    native_snapshot_to_dict,
    rollback_native,
)
from .schema import ToolObservation
from .store import init_tuning_db, make_tuning_engine


class SignedObservation(BaseModel):
    observation: ToolObservation
    signature: str | None = None


class ObservationBatch(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    repo_fingerprint: str = Field(min_length=1, max_length=256)
    observations: list[SignedObservation] = Field(default_factory=list)


class RollbackRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    repo_fingerprint: str = Field(min_length=1, max_length=256)


class SignedNativeBatch(BaseModel):
    manifest: NativeTelemetryManifest
    signature: str = Field(min_length=64, max_length=64)


def create_app(db_url: str | None = None) -> FastAPI:
    engine = make_tuning_engine(db_url)
    init_tuning_db(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)

    def get_db() -> Any:
        db: Session = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI(title="clawbox-tune-kb")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/v1/kb/generation", dependencies=[Depends(require_service_token)])
    def get_generation(tenant_id: str, repo: str, db: Session = Depends(get_db)):
        meta = snapshot_metadata(db, tenant_id=tenant_id, repo_fingerprint=repo)
        if meta is None:
            return {"tenant_id": tenant_id, "repo_fingerprint": repo, "generation": 0}
        return meta

    @app.get("/v1/kb/snapshot", dependencies=[Depends(require_service_token)])
    def get_snapshot(
        tenant_id: str,
        repo: str,
        format: Literal["research", "clawtune"] = "research",
        db: Session = Depends(get_db),
    ):
        row = latest_snapshot(db, tenant_id=tenant_id, repo_fingerprint=repo)
        if row is None:
            raise HTTPException(status_code=404, detail="no snapshot for (tenant, repo)")
        data = snapshot_row_to_dict(row, parse_snapshots=True)
        if format == "clawtune":
            return {
                "tenant_id": row.tenant_id,
                "repo_fingerprint": row.repo_fingerprint,
                "generation": row.generation,
                "input_digest": row.input_digest,
                "input_count": row.input_count,
                "created_at": data["created_at"],
                "snapshot": data["clawtune_snapshot"],
            }
        return data

    @app.post("/v1/kb/observations", dependencies=[Depends(require_service_token)])
    def post_observations(body: ObservationBatch, db: Session = Depends(get_db)):
        observations = [item.observation for item in body.observations]
        signatures = {}
        for item in body.observations:
            if item.signature:
                signatures[(item.observation.execution_id, item.observation.tool_name, item.observation.sequence_no)] = item.signature
        outcome = ingest(
            db,
            tenant_id=body.tenant_id,
            repo_fingerprint=body.repo_fingerprint,
            observations=observations,
            signatures=signatures,
            ingest_secret=settings.kb_ingest_secret or settings.ingest_secret,
        )
        db.commit()
        return outcome.to_dict()

    @app.post("/v1/kb/rollback", dependencies=[Depends(require_service_token)])
    def post_rollback(body: RollbackRequest, db: Session = Depends(get_db)):
        generation = rollback(db, tenant_id=body.tenant_id, repo_fingerprint=body.repo_fingerprint)
        db.commit()
        return {
            "tenant_id": body.tenant_id,
            "repo_fingerprint": body.repo_fingerprint,
            "generation": generation,
        }

    @app.post("/v1/kb/native-batches", dependencies=[Depends(require_service_token)])
    def post_native_batch(body: SignedNativeBatch, db: Session = Depends(get_db)):
        outcome = ingest_native_batch(
            db,
            manifest=body.manifest,
            signature=body.signature,
            ingest_secret=settings.kb_ingest_secret or settings.ingest_secret,
            expected_clawtune_revision=settings.clawtune_revision,
        )
        db.commit()
        return outcome.to_dict()

    @app.get("/v1/kb/native-snapshot", dependencies=[Depends(require_service_token)])
    def get_native_snapshot(
        tenant_id: str, repo: str, db: Session = Depends(get_db)
    ):
        row = latest_native_snapshot(
            db, tenant_id=tenant_id, repo_fingerprint=repo
        )
        if row is None:
            raise HTTPException(status_code=404, detail="no native snapshot")
        return native_snapshot_to_dict(row)

    @app.get("/v1/kb/admission-prediction", dependencies=[Depends(require_service_token)])
    def get_admission_prediction(
        tenant_id: str,
        repo: str,
        generation: int | None = None,
        db: Session = Depends(get_db),
    ):
        """Return an authoritative repository-level p90 for Cell admission.

        A Cell is sized before OpenClaw reveals its future commands.  Querying
        the native runtime KB with an outer ``exec`` call and no command
        therefore intentionally selects its repository corpus' coarse
        tool/global node.  Static studies request an exact immutable
        generation; elastic studies omit it and receive the latest generation.
        """
        try:
            row = (
                native_snapshot_for_generation(
                    db, tenant_id=tenant_id, repo_fingerprint=repo,
                    generation=generation,
                )
                if generation is not None
                else latest_native_snapshot(
                    db, tenant_id=tenant_id, repo_fingerprint=repo,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if row is None:
            qualifier = f" generation {generation}" if generation is not None else ""
            raise HTTPException(
                status_code=404,
                detail=f"no native snapshot{qualifier} for (tenant, repo)",
            )
        _, _, RuntimeToolResourceKB, ToolCallQuery, _, _ = _clawtune_api()
        runtime_snapshot = json.loads(row.runtime_snapshot)
        kb = RuntimeToolResourceKB.from_json_obj(runtime_snapshot)
        predictions = kb.query(ToolCallQuery(
            repo=repo,
            tool_name="exec",
            command=None,
            ts_start=max(
                time.time(), float(runtime_snapshot.get("last_query_ts") or 0.0),
            ),
            ambient_before_mb=0.0,
        ))
        latency = predictions["latency_ms"]
        cpu = predictions["peak_cpu_cores"]
        memory = predictions["peak_memory_mb"]
        values = (latency.conditional_p90, cpu.conditional_p90, memory.conditional_p90)
        if any(value is None or not math.isfinite(float(value)) or float(value) <= 0 for value in values):
            raise HTTPException(status_code=409, detail="native snapshot has no safe positive p90")
        return {
            "tenant_id": row.tenant_id,
            "repo_fingerprint": row.repo_fingerprint,
            "generation": row.generation,
            "pair_digest": row.pair_digest,
            "source_digest": row.source_digest,
            "artifact_count": row.artifact_count,
            "clawtune_revision": row.clawtune_revision,
            "prediction": {
                "latency_p90_sec": float(latency.conditional_p90) / 1000.0,
                "cpu_p90_cores": float(cpu.conditional_p90),
                "memory_p90_bytes": float(memory.conditional_p90) * 1024.0 * 1024.0,
                "evidence_count": min(
                    latency.evidence_count, cpu.evidence_count, memory.evidence_count,
                ),
                "scopes": {
                    "latency": latency.scope,
                    "cpu": cpu.scope,
                    "memory": memory.scope,
                },
                "fallback_paths": {
                    "latency": list(latency.fallback_path),
                    "cpu": list(cpu.fallback_path),
                    "memory": list(memory.fallback_path),
                },
            },
        }

    @app.post("/v1/kb/native-rollback", dependencies=[Depends(require_service_token)])
    def post_native_rollback(body: RollbackRequest, db: Session = Depends(get_db)):
        generation = rollback_native(
            db,
            tenant_id=body.tenant_id,
            repo_fingerprint=body.repo_fingerprint,
        )
        db.commit()
        return {
            "tenant_id": body.tenant_id,
            "repo_fingerprint": body.repo_fingerprint,
            "generation": generation,
        }

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("TUNING_API_HOST", "0.0.0.0"),
        port=int(os.getenv("TUNING_API_PORT", "8086")),
    )


if __name__ == "__main__":
    main()
