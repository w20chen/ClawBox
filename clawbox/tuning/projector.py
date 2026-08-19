"""Control-plane projector: signed observations -> trusted store -> KB (P1).

The projector is the write path of the observe → KB → shadow-predict loop.
A runtime pod (or offline script) POSTs a batch of signed ``ToolObservation``s;
the projector:

1. validates each observation (schema / identity / HMAC signature via the
   ``ingest_secret`` / quality gate),
2. deduplicates against the append-only store on
   ``(tenant_id, repo_fingerprint, execution_id, tool_name, sequence_no)``
   (replays are idempotent: repeated keys are dropped, never re-trained),
3. appends the newly trusted observations,
4. rebuilds the immutable ``(tenant, repo)`` KB snapshot — both the research
   format and the ClawTune-loadable format — as generation++, and persists it.

Concurrency: the trusted store is append-only and the snapshot is a pure
function of the observation set, so concurrent ingests cannot lose
observations.  Generation numbers are monotonic: each ingest computes the
next generation inside its own transaction (last-writer-wins; a concurrent
build just produces an extra rollback-able generation).

Cross-tenant isolation is structural: every query and every build is scoped
by ``(tenant_id, repo_fingerprint)``; one tenant's observations never train
another tenant's snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .clawtune import build_clawtune_kb_snapshot
from .kb import KnowledgeBase, KnowledgeBaseBuilder
from .schema import ToolObservation
from .store import (
    TuningKBSnapshotRow,
    TuningObservationRow,
    json_dumps,
    observation_dedup_key,
    observation_to_payload,
    payload_to_observation,
    row_to_observation,
    utcnow,
)
from .validate import Deduplicator, ObservationValidator, ValidationResult


@dataclass
class IngestOutcome:
    """Result of one signed batch ingest."""

    generation: int | None
    accepted: int = 0
    duplicates: int = 0
    rejected: list[tuple[ToolObservation, str]] = field(default_factory=list)
    new_snapshot: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": [
                {
                    "execution_id": obs.execution_id,
                    "tool_name": obs.tool_name,
                    "sequence_no": obs.sequence_no,
                    "reason": reason,
                }
                for obs, reason in self.rejected
            ],
            "new_snapshot": self.new_snapshot,
        }


def _existing_dedup_keys(
    db: Session, *, tenant_id: str, repo_fingerprint: str
) -> set[tuple[str, str, int]]:
    rows = db.scalars(
        select(TuningObservationRow).where(
            TuningObservationRow.tenant_id == tenant_id,
            TuningObservationRow.repo_fingerprint == repo_fingerprint,
        )
    ).all()
    return {observation_dedup_key(row_to_observation(row)) for row in rows}


def _latest_generation(db: Session, *, tenant_id: str, repo_fingerprint: str) -> int:
    value = db.scalar(
        select(TuningKBSnapshotRow.generation)
        .where(
            TuningKBSnapshotRow.tenant_id == tenant_id,
            TuningKBSnapshotRow.repo_fingerprint == repo_fingerprint,
        )
        .order_by(TuningKBSnapshotRow.generation.desc())
        .limit(1)
    )
    return int(value) if value is not None else 0


def _all_trusted_observations(
    db: Session, *, tenant_id: str, repo_fingerprint: str
) -> list[ToolObservation]:
    rows = db.scalars(
        select(TuningObservationRow)
        .where(
            TuningObservationRow.tenant_id == tenant_id,
            TuningObservationRow.repo_fingerprint == repo_fingerprint,
        )
        .order_by(TuningObservationRow.id)
    ).all()
    return [row_to_observation(row) for row in rows]


def _rebuild_snapshot(
    db: Session, *, tenant_id: str, repo_fingerprint: str
) -> int:
    """Rebuild (tenant, repo) KB from the full trusted set; returns generation."""
    observations = _all_trusted_observations(db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint)
    builder = KnowledgeBaseBuilder()
    builder.add_many(observations)
    kb = builder.build()
    clawtune = build_clawtune_kb_snapshot(observations, repo_fingerprint)
    generation = _latest_generation(db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint) + 1
    db.add(
        TuningKBSnapshotRow(
            tenant_id=tenant_id,
            repo_fingerprint=repo_fingerprint,
            generation=generation,
            snapshot=kb.to_json(),
            clawtune_snapshot=json_dumps(clawtune),
            input_digest=kb.metadata.input_digest,
            input_count=kb.metadata.input_count,
            created_at=utcnow(),
        )
    )
    return generation


def ingest(
    db: Session,
    *,
    tenant_id: str,
    repo_fingerprint: str,
    observations: list[ToolObservation],
    signatures: dict[tuple[str, str, int] | str, str] | None = None,
    ingest_secret: str | None = None,
) -> IngestOutcome:
    """Validate + dedup + append + rebuild a signed observation batch.

    Parameters
    ----------
    signatures:
        Optional mapping keyed by the dedup key
        ``(execution_id, tool_name, sequence_no)`` or by ``execution_id`` to
        the HMAC signature carried alongside each observation.
    """
    signatures = signatures or {}
    validator = ObservationValidator(ingest_secret)
    existing = _existing_dedup_keys(db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint)
    batch_dedup = Deduplicator()
    outcome = IngestOutcome(generation=_latest_generation(db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint))

    to_insert: list[TuningObservationRow] = []
    rejected: list[tuple[ToolObservation, str]] = []
    for observation in observations:
        sig = signatures.get(observation_dedup_key(observation)) or signatures.get(observation.execution_id)
        result: ValidationResult = validator.validate(observation, sig)
        if not result.valid:
            rejected.append((observation, result.reason or "validation failed"))
            continue
        key = observation_dedup_key(observation)
        if key in existing:
            outcome.duplicates += 1
            continue
        if not batch_dedup.claim(observation):
            outcome.duplicates += 1
            continue
        trusted = observation.model_copy(update={"trusted": True})
        to_insert.append(
            TuningObservationRow(
                tenant_id=tenant_id,
                repo_fingerprint=repo_fingerprint,
                execution_id=trusted.execution_id,
                tool_name=trusted.tool_name,
                sequence_no=trusted.sequence_no,
                payload=observation_to_payload(trusted),
                created_at=trusted.created_at or utcnow(),
            )
        )

    outcome.rejected = rejected
    if not to_insert:
        return outcome

    for row in to_insert:
        db.add(row)
    db.flush()
    outcome.accepted = len(to_insert)
    outcome.generation = _rebuild_snapshot(
        db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint
    )
    outcome.new_snapshot = True
    return outcome


def latest_snapshot(
    db: Session, *, tenant_id: str, repo_fingerprint: str
) -> TuningKBSnapshotRow | None:
    return db.scalar(
        select(TuningKBSnapshotRow)
        .where(
            TuningKBSnapshotRow.tenant_id == tenant_id,
            TuningKBSnapshotRow.repo_fingerprint == repo_fingerprint,
        )
        .order_by(TuningKBSnapshotRow.generation.desc())
        .limit(1)
    )


def snapshot_row_to_dict(
    row: TuningKBSnapshotRow, *, parse_snapshots: bool = True
) -> dict[str, Any]:
    """Full snapshot row as a JSON-ready dict (server GET payload)."""
    data: dict[str, Any] = {
        "tenant_id": row.tenant_id,
        "repo_fingerprint": row.repo_fingerprint,
        "generation": row.generation,
        "input_digest": row.input_digest,
        "input_count": row.input_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if parse_snapshots:
        data["snapshot"] = __import__("json").loads(row.snapshot)
        if row.clawtune_snapshot:
            data["clawtune_snapshot"] = __import__("json").loads(row.clawtune_snapshot)
        else:
            data["clawtune_snapshot"] = None
    else:
        data["snapshot"] = row.snapshot
        data["clawtune_snapshot"] = row.clawtune_snapshot
    return data


def snapshot_metadata(
    db: Session, *, tenant_id: str, repo_fingerprint: str
) -> dict[str, Any] | None:
    row = latest_snapshot(db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint)
    if row is None:
        return None
    return {
        "tenant_id": row.tenant_id,
        "repo_fingerprint": row.repo_fingerprint,
        "generation": row.generation,
        "input_digest": row.input_digest,
        "input_count": row.input_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def rollback(
    db: Session, *, tenant_id: str, repo_fingerprint: str
) -> int:
    """Drop the latest generation; returns the new latest generation (0 if none)."""
    latest = latest_snapshot(db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint)
    if latest is None:
        return 0
    db.delete(latest)
    db.flush()
    return _latest_generation(db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint)


def observations_for(
    db: Session, *, tenant_id: str, repo_fingerprint: str
) -> list[ToolObservation]:
    """All trusted observations for a (tenant, repo) — for audit / rebuild."""
    return _all_trusted_observations(db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint)


def load_kb(
    db: Session, *, tenant_id: str, repo_fingerprint: str
) -> KnowledgeBase | None:
    row = latest_snapshot(db, tenant_id=tenant_id, repo_fingerprint=repo_fingerprint)
    if row is None:
        return None
    return KnowledgeBase.from_snapshot(__import__("json").loads(row.snapshot))
