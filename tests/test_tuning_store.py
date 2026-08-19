"""Tests for the tuning control-plane store + projector (P1)."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from clawbox.tuning.kb import KnowledgeBase
from clawbox.tuning.projector import (
    ingest,
    latest_snapshot,
    load_kb,
    observations_for,
    rollback,
    snapshot_metadata,
)
from clawbox.tuning.schema import CollectionQuality, ToolObservation, utcnow
from clawbox.tuning.store import (
    TuningObservationRow,
    init_tuning_db,
    make_tuning_engine,
    row_to_observation,
)
from clawbox.tuning.validate import sign_observation

SECRET = "test-ingest-secret"


def make_obs(
    execution_id: str,
    *,
    tool_name: str = "exec",
    command: str = "python -m pytest -q",
    duration_sec: float = 5.0,
    seq: int = 0,
    exit_code: int = 0,
    cpu_cores: float = 1.5,
    rss_bytes: int = 1024**2,
    quality: CollectionQuality = CollectionQuality.VALID,
    complete: bool = True,
    repo: str = "github.com/acme/foo",
) -> ToolObservation:
    end = utcnow()
    start = end - timedelta(seconds=duration_sec)
    return ToolObservation(
        execution_id=execution_id,
        tool_name=tool_name,
        command=command,
        command_digest="sha256-" + execution_id[-8:],
        repo_fingerprint=repo,
        run_id=f"run-{execution_id}",
        sequence_no=seq,
        start_time=start,
        end_time=end,
        duration_sec=duration_sec,
        status_code="ok" if exit_code == 0 else "error",
        exit_code=exit_code,
        complete=complete,
        collection_quality=quality,
        coverage_ratio=1.0,
        coverage_reason="full_window",
        cpu_time_sec=duration_sec * cpu_cores,
        cpu_utilization_avg_cores=cpu_cores,
        rss_peak_bytes=rss_bytes,
        memory_rss_bytes_after=rss_bytes,
    )


def sign_batch(observations, secret: str = SECRET):
    return {
        (obs.execution_id, obs.tool_name, obs.sequence_no): sign_observation(obs, secret)
        for obs in observations
    }


@pytest.fixture()
def db(tmp_path):
    engine = make_tuning_engine(f"sqlite:///{tmp_path}/tuning.db")
    init_tuning_db(engine)
    from sqlalchemy.orm import sessionmaker

    session_factory = sessionmaker(engine, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ── store ──────────────────────────────────────────────────────────────

def test_observation_row_roundtrip(db):
    obs = make_obs("exec-0001")
    from clawbox.tuning.store import observation_to_payload

    db.add(
        TuningObservationRow(
            tenant_id="tenant-a",
            repo_fingerprint="github.com/acme/foo",
            execution_id=obs.execution_id,
            tool_name=obs.tool_name,
            sequence_no=obs.sequence_no,
            payload=observation_to_payload(obs),
            created_at=obs.created_at,
        )
    )
    db.commit()
    row = db.scalar(select(TuningObservationRow))
    assert row is not None
    restored = row_to_observation(row)
    assert restored.execution_id == "exec-0001"
    assert restored.duration_sec == obs.duration_sec
    assert restored.trusted is False


# ── projector ──────────────────────────────────────────────────────────

def test_ingest_accepts_valid_signed_batch(db):
    obs = [make_obs(f"exec-{i:04d}") for i in range(2)]
    outcome = ingest(
        db,
        tenant_id="tenant-a",
        repo_fingerprint="github.com/acme/foo",
        observations=obs,
        signatures=sign_batch(obs),
        ingest_secret=SECRET,
    )
    db.commit()
    assert outcome.accepted == 2
    assert outcome.duplicates == 0
    assert not outcome.rejected
    assert outcome.generation == 1
    assert outcome.new_snapshot is True
    row = latest_snapshot(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo")
    assert row is not None
    assert row.generation == 1
    assert row.input_count == 2
    assert len(observations_for(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo")) == 2


def test_ingest_rejects_invalid_observations(db):
    valid = make_obs("exec-1000")
    bad_sig = make_obs("exec-1001")
    incomplete = make_obs("exec-1002", complete=False)
    nonzero = make_obs("exec-1003", exit_code=1)
    bad_quality = make_obs("exec-1004", quality=CollectionQuality.DEGRADED)
    signatures = sign_batch([valid, incomplete, nonzero, bad_quality])
    signatures[(bad_sig.execution_id, bad_sig.tool_name, bad_sig.sequence_no)] = "deadbeef"
    outcome = ingest(
        db,
        tenant_id="tenant-a",
        repo_fingerprint="github.com/acme/foo",
        observations=[valid, bad_sig, incomplete, nonzero, bad_quality],
        signatures=signatures,
        ingest_secret=SECRET,
    )
    db.commit()
    assert outcome.accepted == 1
    assert len(outcome.rejected) == 4
    reasons = {reason for _, reason in outcome.rejected}
    assert "invalid HMAC signature" in reasons
    assert any("incomplete" in reason for reason in reasons)
    assert any("exit_code" in reason for reason in reasons)
    assert any("collection_quality" in reason for reason in reasons)


def test_ingest_replay_is_idempotent(db):
    obs = [make_obs(f"exec-{i:04d}") for i in range(3)]
    first = ingest(
        db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo",
        observations=obs, signatures=sign_batch(obs), ingest_secret=SECRET,
    )
    db.commit()
    second = ingest(
        db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo",
        observations=obs, signatures=sign_batch(obs), ingest_secret=SECRET,
    )
    db.commit()
    assert first.generation == 1
    assert second.accepted == 0
    assert second.duplicates == 3
    assert second.generation == 1  # no new snapshot on a pure replay
    assert second.new_snapshot is False


def test_generation_monotonic_across_batches(db):
    batch1 = [make_obs("exec-2000"), make_obs("exec-2001")]
    batch2 = [make_obs("exec-2002"), make_obs("exec-2003")]
    r1 = ingest(
        db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo",
        observations=batch1, signatures=sign_batch(batch1), ingest_secret=SECRET,
    )
    db.commit()
    r2 = ingest(
        db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo",
        observations=batch2, signatures=sign_batch(batch2), ingest_secret=SECRET,
    )
    db.commit()
    assert r1.generation == 1
    assert r2.generation == 2
    row = latest_snapshot(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo")
    assert row.input_count == 4
    assert row.generation == 2
    kb = load_kb(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo")
    assert isinstance(kb, KnowledgeBase)
    assert kb.metadata.input_digest == row.input_digest


def test_cross_tenant_isolation(db):
    obs_a = [make_obs("exec-3000", repo="github.com/acme/foo")]
    obs_b = [make_obs("exec-3100", repo="github.com/acme/foo")]
    ingest(
        db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo",
        observations=obs_a, signatures=sign_batch(obs_a), ingest_secret=SECRET,
    )
    db.commit()
    ingest(
        db, tenant_id="tenant-b", repo_fingerprint="github.com/acme/foo",
        observations=obs_b, signatures=sign_batch(obs_b), ingest_secret=SECRET,
    )
    db.commit()
    # tenant A's snapshot contains only A's observation.
    meta_a = snapshot_metadata(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo")
    assert meta_a["input_count"] == 1
    obs_a_rows = observations_for(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo")
    assert [o.execution_id for o in obs_a_rows] == ["exec-3000"]
    # A's observations never appear under tenant B.
    obs_b_rows = observations_for(db, tenant_id="tenant-b", repo_fingerprint="github.com/acme/foo")
    assert [o.execution_id for o in obs_b_rows] == ["exec-3100"]


def test_rollback_returns_previous_generation(db):
    batch1 = [make_obs("exec-4000")]
    batch2 = [make_obs("exec-4001")]
    ingest(
        db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo",
        observations=batch1, signatures=sign_batch(batch1), ingest_secret=SECRET,
    )
    db.commit()
    ingest(
        db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo",
        observations=batch2, signatures=sign_batch(batch2), ingest_secret=SECRET,
    )
    db.commit()
    assert snapshot_metadata(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo")["generation"] == 2
    gen = rollback(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo")
    db.commit()
    assert gen == 1
    gen = rollback(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo")
    db.commit()
    assert gen == 0
    assert latest_snapshot(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo") is None


def test_clawtune_snapshot_loadable_shape(db):
    obs = [
        make_obs("exec-5000", command="make -j12 all"),
        make_obs("exec-5001", command="pytest -q --timeout 30"),
    ]
    ingest(
        db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo",
        observations=obs, signatures=sign_batch(obs), ingest_secret=SECRET,
    )
    db.commit()
    row = latest_snapshot(db, tenant_id="tenant-a", repo_fingerprint="github.com/acme/foo")
    import json

    clawtune = json.loads(row.clawtune_snapshot)
    assert clawtune["schema"] == "runtime_tool_resource_kb_v1"
    assert clawtune["quantile"] == 0.9
    assert set(clawtune["public"]) == {"latency_ms", "peak_cpu_cores", "peak_memory_mb"}
    assert "github.com/acme/foo" in clawtune["repo"]
    # Node rows are [kind, key, [values...]].
    for target, nodes in clawtune["repo"]["github.com/acme/foo"].items():
        for kind, key, values in nodes:
            assert isinstance(kind, str)
            assert isinstance(key, str)
            assert isinstance(values, list) and all(isinstance(v, float) for v in values)
    # pending must be empty and last_query_ts null for a clean snapshot.
    assert clawtune["pending"] == []
    assert clawtune["last_query_ts"] is None


def test_concurrent_ingest_loses_no_observations(tmp_path):
    import threading

    engine = make_tuning_engine(f"sqlite:///{tmp_path}/conc.db")
    init_tuning_db(engine)
    from sqlalchemy.orm import sessionmaker

    session_factory = sessionmaker(engine, expire_on_commit=False)
    tenant = "tenant-a"
    repo = "github.com/acme/foo"
    threads = 8
    per_thread = 5
    errors: list[BaseException] = []
    barrier = threading.Barrier(threads)

    def worker(worker_id: int) -> None:
        session = session_factory()
        try:
            obs = [
                make_obs(f"conc-{worker_id}-{j:02d}", repo=repo)
                for j in range(per_thread)
            ]
            barrier.wait()
            outcome = ingest(
                session,
                tenant_id=tenant,
                repo_fingerprint=repo,
                observations=obs,
                signatures=sign_batch(obs),
                ingest_secret=SECRET,
            )
            session.commit()
        except BaseException as exc:  # pragma: no cover - failure reporting
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert not errors, errors
    session = session_factory()
    try:
        stored = observations_for(session, tenant_id=tenant, repo_fingerprint=repo)
        meta = snapshot_metadata(session, tenant_id=tenant, repo_fingerprint=repo)
    finally:
        session.close()
    assert len(stored) == threads * per_thread
    assert meta["input_count"] == threads * per_thread
    # Each ingest with new observations produced one generation.
    assert meta["generation"] == threads
