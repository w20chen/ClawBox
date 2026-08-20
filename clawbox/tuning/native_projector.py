"""Transactional native artifact store and ClawTune KB projector."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .native import (
    NativeTelemetryManifest,
    canonical_manifest,
    manifest_digest,
    project_native_manifests,
    verify_native_manifest,
)
from .store import (
    TuningNativeArtifactRow,
    TuningNativeBatchRow,
    TuningNativeSnapshotRow,
    json_dumps,
    utcnow,
)


@dataclass(frozen=True)
class NativeIngestOutcome:
    generation: int
    accepted: bool = False
    duplicate: bool = False
    rejection_reason: str | None = None
    manifest_digest: str | None = None
    source_digest: str | None = None
    pair_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "rejection_reason": self.rejection_reason,
            "manifest_digest": self.manifest_digest,
            "source_digest": self.source_digest,
            "pair_digest": self.pair_digest,
        }


def latest_native_snapshot(
    db: Session, *, tenant_id: str, repo_fingerprint: str
) -> TuningNativeSnapshotRow | None:
    return db.scalar(
        select(TuningNativeSnapshotRow)
        .where(
            TuningNativeSnapshotRow.tenant_id == tenant_id,
            TuningNativeSnapshotRow.repo_fingerprint == repo_fingerprint,
        )
        .order_by(TuningNativeSnapshotRow.generation.desc())
        .limit(1)
    )


def _generation(db: Session, tenant_id: str, repo_fingerprint: str) -> int:
    row = latest_native_snapshot(
        db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint
    )
    return row.generation if row is not None else 0


def _accepted_manifests(
    db: Session, tenant_id: str, repo_fingerprint: str
) -> list[NativeTelemetryManifest]:
    rows = db.scalars(
        select(TuningNativeBatchRow)
        .where(
            TuningNativeBatchRow.tenant_id == tenant_id,
            TuningNativeBatchRow.repo_fingerprint == repo_fingerprint,
            TuningNativeBatchRow.status == "accepted",
        )
        .order_by(TuningNativeBatchRow.id)
    ).all()
    return [NativeTelemetryManifest.model_validate_json(row.manifest) for row in rows]


def ingest_native_batch(
    db: Session,
    *,
    manifest: NativeTelemetryManifest,
    signature: str,
    ingest_secret: str,
    expected_clawtune_revision: str,
) -> NativeIngestOutcome:
    """Store raw bytes, validate natively, then publish one atomic KB pair."""

    generation = _generation(db, manifest.tenant_id, manifest.repo_fingerprint)
    digest = manifest_digest(manifest)
    if not verify_native_manifest(manifest, ingest_secret, signature):
        return NativeIngestOutcome(
            generation=generation,
            rejection_reason="invalid native manifest HMAC signature",
            manifest_digest=digest,
        )
    existing = db.scalar(
        select(TuningNativeBatchRow).where(
            TuningNativeBatchRow.tenant_id == manifest.tenant_id,
            TuningNativeBatchRow.repo_fingerprint == manifest.repo_fingerprint,
            TuningNativeBatchRow.manifest_digest == digest,
        )
    )
    if existing is not None:
        return NativeIngestOutcome(
            generation=generation,
            duplicate=True,
            rejection_reason=existing.rejection_reason,
            manifest_digest=digest,
        )

    batch = TuningNativeBatchRow(
        tenant_id=manifest.tenant_id,
        repo_fingerprint=manifest.repo_fingerprint,
        manifest_digest=digest,
        manifest=canonical_manifest(manifest).decode("utf-8"),
        signature=signature,
        run_id=manifest.run_id,
        attempt_id=manifest.attempt_id,
        cell_id=manifest.cell_id,
        clawtune_revision=manifest.clawtune_revision,
        status="validating",
        rejection_reason=None,
        created_at=utcnow(),
    )
    db.add(batch)
    db.flush()

    accepted_digests = {
        item.sha256
        for accepted_manifest in _accepted_manifests(
            db, manifest.tenant_id, manifest.repo_fingerprint
        )
        for item in accepted_manifest.artifacts
    }
    reused = sorted({item.sha256 for item in manifest.artifacts} & accepted_digests)
    if reused:
        reason = "artifact digest already belongs to another manifest"
        batch.status = "rejected"
        batch.rejection_reason = reason
        return NativeIngestOutcome(
            generation=generation,
            rejection_reason=reason,
            manifest_digest=digest,
        )

    try:
        for item in manifest.artifacts:
            # Digest/base64 validation occurs before the raw record is flushed.
            item.raw_bytes()
    except ValueError as exc:
        reason = f"native artifact envelope failed: {exc}"
        batch.status = "rejected"
        batch.rejection_reason = reason
        return NativeIngestOutcome(
            generation=generation,
            rejection_reason=reason,
            manifest_digest=digest,
        )

    stored_digests = set(
        db.scalars(
            select(TuningNativeArtifactRow.artifact_digest).where(
                TuningNativeArtifactRow.tenant_id == manifest.tenant_id,
                TuningNativeArtifactRow.repo_fingerprint
                == manifest.repo_fingerprint,
            )
        ).all()
    )
    for item in manifest.artifacts:
        if item.sha256 in stored_digests:
            continue
        db.add(
            TuningNativeArtifactRow(
                batch_id=batch.id,
                tenant_id=manifest.tenant_id,
                repo_fingerprint=manifest.repo_fingerprint,
                execution_id=item.execution_id,
                kind=item.kind,
                filename=item.filename,
                artifact_digest=item.sha256,
                content_b64=item.content_b64,
                created_at=utcnow(),
            )
        )
    db.flush()  # raw immutable storage exists before native validation/projecting

    if manifest.clawtune_revision != expected_clawtune_revision:
        reason = (
            f"incompatible ClawTune revision {manifest.clawtune_revision}; "
            f"expected {expected_clawtune_revision}"
        )
        batch.status = "rejected"
        batch.rejection_reason = reason
        return NativeIngestOutcome(
            generation=generation,
            rejection_reason=reason,
            manifest_digest=digest,
        )

    try:
        projection = project_native_manifests(
            [
                *_accepted_manifests(
                    db, manifest.tenant_id, manifest.repo_fingerprint
                ),
                manifest,
            ]
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        reason = f"native validation failed: {type(exc).__name__}: {exc}"
        batch.status = "rejected"
        batch.rejection_reason = reason
        return NativeIngestOutcome(
            generation=generation,
            rejection_reason=reason,
            manifest_digest=digest,
        )

    clause_json = json_dumps(projection.clause_snapshot)
    runtime_json = json_dumps(projection.runtime_snapshot)
    pair_digest = hashlib.sha256(
        (clause_json + "\n" + runtime_json).encode("utf-8")
    ).hexdigest()
    generation += 1
    db.add(
        TuningNativeSnapshotRow(
            tenant_id=manifest.tenant_id,
            repo_fingerprint=manifest.repo_fingerprint,
            generation=generation,
            clause_snapshot=clause_json,
            runtime_snapshot=runtime_json,
            pair_digest=pair_digest,
            source_digest=projection.source_digest,
            artifact_count=projection.artifact_count,
            evidence=json_dumps(projection.evidence),
            clawtune_revision=manifest.clawtune_revision,
            created_at=utcnow(),
        )
    )
    batch.status = "accepted"
    batch.rejection_reason = None
    return NativeIngestOutcome(
        generation=generation,
        accepted=True,
        manifest_digest=digest,
        source_digest=projection.source_digest,
        pair_digest=pair_digest,
    )


def native_snapshot_to_dict(row: TuningNativeSnapshotRow) -> dict[str, Any]:
    return {
        "tenant_id": row.tenant_id,
        "repo_fingerprint": row.repo_fingerprint,
        "generation": row.generation,
        "pair_digest": row.pair_digest,
        "source_digest": row.source_digest,
        "artifact_count": row.artifact_count,
        "clawtune_revision": row.clawtune_revision,
        "created_at": row.created_at.isoformat(),
        "clause_snapshot": json.loads(row.clause_snapshot),
        "runtime_snapshot": json.loads(row.runtime_snapshot),
        "evidence": json.loads(row.evidence),
    }


def rollback_native(
    db: Session, *, tenant_id: str, repo_fingerprint: str
) -> int:
    """Atomically remove the latest paired generation."""

    row = latest_native_snapshot(
        db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint
    )
    if row is None:
        return 0
    db.delete(row)
    db.flush()
    return _generation(db, tenant_id, repo_fingerprint)
