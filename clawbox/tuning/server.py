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
        command: str,
        generation: int | None = None,
        db: Session = Depends(get_db),
    ):
        """Return native LatticeKB P90s for a concrete command and generation."""
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
        (
            ClauseResourceKB, _, RuntimeToolResourceKB, ToolCallQuery,
            _, _, LatticeTimeKB,
        ) = _clawtune_api()
        runtime_snapshot = json.loads(row.runtime_snapshot)
        kb = RuntimeToolResourceKB.from_json_obj(runtime_snapshot)
        clause_kb = ClauseResourceKB.from_json_obj(json.loads(row.clause_snapshot))
        lattice_kb = LatticeTimeKB.from_json_obj(json.loads(row.lattice_snapshot))
        query = ToolCallQuery(
            repo=repo,
            tool_name="exec",
            command=command,
            ts_start=max(
                time.time(), float(runtime_snapshot.get("last_query_ts") or 0.0),
            ),
            memory_measurement="guest_memtotal_minus_memavailable",
        )
        from .clawtune import predict_native_call_load_models
        models = predict_native_call_load_models(
            kb, query, clause=clause_kb, lattice=lattice_kb,
        )
        call_load = models["lattice"]
        latency = call_load.targets["duration_ms"]
        cpu = call_load.targets["cpu_avg_cores"]
        memory = call_load.targets["memory_extra_peak_bytes"]
        values = (latency.p90, cpu.p90, memory.p90)
        if any(value is None or not math.isfinite(float(value)) or float(value) <= 0 for value in values):
            raise HTTPException(status_code=409, detail="LatticeKB has no safe positive P90 for this command and measurement")
        return {
            "tenant_id": row.tenant_id,
            "repo_fingerprint": row.repo_fingerprint,
            "generation": row.generation,
            "pair_digest": row.pair_digest,
            "source_digest": row.source_digest,
            "artifact_count": row.artifact_count,
            "clawtune_revision": row.clawtune_revision,
            "call_prediction": call_load.model_dump(mode="json"),
            "model_predictions": {
                "tool": models["tool"].model_dump(mode="json"),
                "trie": models["trie"].model_dump(mode="json"),
                "lattice": models["lattice"].model_dump(mode="json"),
                "diagnostics": models["diagnostics"].model_dump(mode="json"),
            },
            "prediction": {
                "cpu_metric": "cpu_avg_cores",
                "memory_metric": "environment_memory_peak_minus_baseline",
                "latency_p90_sec": float(latency.p90) / 1000.0,
                "cpu_p90_cores": float(cpu.p90),
                "memory_p90_bytes": float(memory.p90),
                "evidence_count": min(
                    latency.sample_count, cpu.sample_count, memory.sample_count,
                ),
                "scopes": {
                    "latency": latency.context[0] if latency.context else None,
                    "cpu": cpu.context[0] if cpu.context else None,
                    "memory": memory.context[0] if memory.context else None,
                },
                "fallback_paths": {
                    "latency": list(latency.context),
                    "cpu": list(cpu.context),
                    "memory": list(memory.context),
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
