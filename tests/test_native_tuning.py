"""Acceptance tests for signed, immutable native ClawTune ingestion."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clawbox.tuning.native import NativeTelemetryManifest, sign_native_manifest
from clawbox.tuning.native_projector import (
    ingest_native_batch,
    latest_native_snapshot,
    native_snapshot_to_dict,
    rollback_native,
)
from clawbox.tuning.store import (
    TuningNativeArtifactRow,
    TuningNativeBatchRow,
    init_tuning_db,
    make_tuning_engine,
)

REVISION = "e91e60bc1e5f3209fbcf6091013fde96f217e2a7"
SECRET = "native-test-secret"
REPO = "github.com/acme/foo"


@pytest.fixture()
def db(tmp_path):
    engine = make_tuning_engine(f"sqlite:///{tmp_path}/native.db")
    init_tuning_db(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _artifact(kind: str, execution_id: str, payload: dict) -> dict:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return {
        "kind": kind,
        "execution_id": execution_id,
        "filename": f"{kind}-{execution_id}.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_b64": base64.b64encode(raw).decode(),
    }


def make_manifest(
    execution_id: str = "exec-a", *, run_id: str = "run-a", tenant: str = "tenant-a",
    repo: str = REPO, revision: str = REVISION, cpu: float = 1.25, rss: int = 16 * 1024**2,
) -> NativeTelemetryManifest:
    start = 1_780_000_000.0 + (10 if execution_id.endswith("b") else 0)
    clause = {
        "version": 2,
        "mode": "clause",
        "status_model": "call_granular_v1",
        "telemetry_quality": "ok",
        "collection_validity": "valid",
        "formal_completeness": "complete",
        "integrity": {"status": "ok", "errors": []},
        "cleanup": "ok",
        "replay_execution": "completed",
        "telemetry_loss_total": {"total": 0},
        "provenance": {"repo": repo},
        "calls": [{
            "tool_call_id": execution_id,
            "command": "python -m pytest -q",
            "eligible_for_kb": True,
            "provenance": {"source_replay_control_flow_fidelity": {"replay_exit_code": 0}},
            "clauses": [{
                "availability": {"latency": "ok", "cpu": "ok", "memory": "ok"},
                "latency_ms": 2000.0,
                "ts_start": start,
                "ts_end": start + 2.0,
                "argv": ["python", "-m", "pytest", "-q"],
                "bin": "python",
                "peak_cpu_cores": cpu,
                "sampled_peak_rss_mb": rss / 1024**2,
                "cpu_ns_cumulative": 1_000_000_000,
                "in_loop": False,
                "in_pipe": False,
                "in_subst": False,
                "pipeline_position": -1,
            }],
        }],
    }
    cgroup = {
        "schema": "cgroup_resource_v1",
        "execution_id": execution_id,
        "tool_name": "exec",
        "source": "cgroup-v2",
        "sampling_quality": "valid",
        "ts_start": start,
        "ts_end": start + 2.0,
        "cpu_utilization_avg_cores": cpu,
        "memory_rss_peak_bytes": rss,
        "collector_errors": [],
        "cgroup_setup_error": None,
        "cgroup_read_error": None,
    }
    return NativeTelemetryManifest.model_validate({
        "schema": "clawbox.native_telemetry_manifest_v1",
        "tenant_id": tenant,
        "repo_fingerprint": repo,
        "run_id": run_id,
        "attempt_id": f"attempt-{run_id}",
        "cell_id": f"cell-{run_id}",
        "collector_version": "guest-collector-v1",
        "clawtune_revision": revision,
        "artifacts": [
            _artifact("clause_telemetry_v2", execution_id, clause),
            _artifact("cgroup_resource_v1", execution_id, cgroup),
        ],
    })


def ingest(db, manifest: NativeTelemetryManifest, *, signature: str | None = None):
    outcome = ingest_native_batch(
        db,
        manifest=manifest,
        signature=signature or sign_native_manifest(manifest, SECRET),
        ingest_secret=SECRET,
        expected_clawtune_revision=REVISION,
    )
    db.commit()
    return outcome


def test_signed_native_ingest_is_immutable_loadable_and_idempotent(db):
    manifest = make_manifest()
    outcome = ingest(db, manifest)
    assert outcome.accepted and outcome.generation == 1
    assert db.scalar(select(func.count()).select_from(TuningNativeArtifactRow)) == 2

    snapshot = native_snapshot_to_dict(latest_native_snapshot(
        db, tenant_id="tenant-a", repo_fingerprint=REPO
    ))
    from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB
    ClauseResourceKB.from_json_obj(snapshot["clause_snapshot"])
    RuntimeToolResourceKB.from_json_obj(snapshot["runtime_snapshot"])
    assert snapshot["artifact_count"] == 2
    assert snapshot["evidence"]["runs"] == ["run-a"]

    replay = ingest(db, manifest)
    assert replay.duplicate and not replay.accepted and replay.generation == 1
    assert db.scalar(select(func.count()).select_from(TuningNativeArtifactRow)) == 2


def test_identity_signature_revision_and_quality_are_hard_gates(db):
    manifest = make_manifest()
    bad_signature = ingest(db, manifest, signature="0" * 64)
    assert not bad_signature.accepted and "HMAC" in bad_signature.rejection_reason
    assert db.scalar(select(func.count()).select_from(TuningNativeBatchRow)) == 0

    wrong_repo = make_manifest(execution_id="exec-b", run_id="run-b", repo="other/repo")
    raw = wrong_repo.model_dump(mode="json", by_alias=True)
    raw["repo_fingerprint"] = REPO
    crossed = NativeTelemetryManifest.model_validate(raw)
    rejected = ingest(db, crossed)
    assert not rejected.accepted and "repository identity" in rejected.rejection_reason

    wrong_revision = make_manifest(execution_id="exec-c", run_id="run-c", revision="0" * 40)
    rejected = ingest(db, wrong_revision)
    assert not rejected.accepted and "incompatible ClawTune revision" in rejected.rejection_reason

    zero_cpu = make_manifest(execution_id="exec-d", run_id="run-d", cpu=0.0)
    rejected = ingest(db, zero_cpu)
    assert not rejected.accepted and "non-positive cpu" in rejected.rejection_reason
    assert latest_native_snapshot(db, tenant_id="tenant-a", repo_fingerprint=REPO) is None


def test_generations_are_cumulative_and_rollback_as_atomic_pairs(db):
    first = ingest(db, make_manifest("exec-a", run_id="run-a"))
    second = ingest(db, make_manifest("exec-b", run_id="run-b"))
    assert (first.generation, second.generation) == (1, 2)
    snapshot = native_snapshot_to_dict(latest_native_snapshot(
        db, tenant_id="tenant-a", repo_fingerprint=REPO
    ))
    assert snapshot["artifact_count"] == 4
    assert snapshot["evidence"]["runs"] == ["run-a", "run-b"]
    assert rollback_native(db, tenant_id="tenant-a", repo_fingerprint=REPO) == 1
    db.commit()
    restored = native_snapshot_to_dict(latest_native_snapshot(
        db, tenant_id="tenant-a", repo_fingerprint=REPO
    ))
    assert restored["generation"] == 1
    assert restored["evidence"]["runs"] == ["run-a"]


def test_rejected_manifest_does_not_poison_corrected_subset(db):
    good = make_manifest("exec-a", run_id="run-a")
    bad = make_manifest("exec-b", run_id="run-a", cpu=0.0)
    combined = good.model_dump(mode="json", by_alias=True)
    combined["artifacts"].extend(
        bad.model_dump(mode="json", by_alias=True)["artifacts"]
    )
    rejected = ingest(db, NativeTelemetryManifest.model_validate(combined))
    assert not rejected.accepted
    accepted = ingest(db, good)
    assert accepted.accepted and accepted.generation == 1
    assert db.scalar(select(func.count()).select_from(TuningNativeArtifactRow)) == 4
    replayed_artifacts = good.model_copy(update={"run_id": "run-later"})
    reused = ingest(db, replayed_artifacts)
    assert not reused.accepted
    assert "already belongs" in reused.rejection_reason
