"""Tests for the ClawTune research pipeline (clawbox/tuning)."""

from __future__ import annotations

import json
from datetime import timedelta

from clawbox.tuning.dataset import build_joined_dataset, export_dataset, read_bridge_jsonl, read_trace_jsonl
from clawbox.tuning.estimators import (
    LatencyBucketClassifier,
    LatencyEstimator,
    MemoryEstimator,
    cross_validate,
    evaluate_predictions,
)
from clawbox.tuning.join import join_trace_and_bridge
from clawbox.tuning.kb import KnowledgeBase, KnowledgeBaseBuilder
from clawbox.tuning.schema import (
    BridgeRecord,
    CollectionQuality,
    ToolObservation,
    duration_bucket,
    span_end_to_observation,
    utcnow,
)
from clawbox.tuning.validate import (
    ObservationValidator,
    classify_observations,
    dedup_key,
    sign_observation,
    verify_signature,
)


# ── Fixture builders ────────────────────────────────────────────────────

def span_end(execution_id, name="exec", status="ok", exit_code=0, duration_sec=5.0,
             cpu_cores=1.5, rss_bytes=1024**2, coverage=1.0, command="python -m pytest -q",
             seq=0):
    wall_ns = int(1_700_000_000_000_000_000)
    return {
        "schema_version": 6,
        "record_type": "span_end",
        "trace_id": f"trace-{execution_id}",
        "span_id": f"span-{execution_id}",
        "parent_span_id": None,
        "session_id": None,
        "run_id": f"run-{execution_id}",
        "agent_id": "agent-1",
        "sequence_no": seq,
        "kind": "tool",
        "name": name,
        "wall_time_ns": str(wall_ns),
        "monotonic_time_ns": "0",
        "duration_ns": str(int(duration_sec * 1e9)),
        "duration_sec": str(duration_sec),
        "repo": "github.com/acme/foo",
        "status": {"code": status, "message": None},
        "output": {"exit_code": exit_code, "result": None},
        "execution": {
            "mode": "in_process_or_runtime_managed",
            "execution_id": execution_id,
            "requested_command": command,
            "payload_command": command,
            "command_digest": f"sha256-{execution_id[-8:]}",
        },
        "resources": {
            "attribution_status": "attributed",
            "scope": "cgroup",
            "quality": "complete",
            "monitor_start_wall_time_ns": str(wall_ns - int(duration_sec * 1e9)),
            "monitor_end_wall_time_ns": str(wall_ns),
            "coverage_ratio": coverage,
            "coverage_reason": "full_window",
            "action_duration_ns": str(int(duration_sec * 1e9)),
            "cpu_time_s": duration_sec * cpu_cores,
            "cpu_utilization_avg_cores": cpu_cores,
            "rss_peak_bytes": rss_bytes,
            "memory_rss_bytes_after": rss_bytes,
            "sampling_interval_ms": 100,
        },
    }


def bridge_record(execution_id, duration_ms=5000, exit_code=0, timed_out=False,
                  stdout_bytes=100, stderr_bytes=20, source="runtime-envelope"):
    return BridgeRecord(
        timestamp="2026-08-19T00:00:00.000Z",
        cell_id="cell-1",
        task_id="task-1",
        execution_id=execution_id,
        execution_source=source,
        command_sha256=f"sha256-{execution_id[-8:]}",
        command_bytes=40,
        duration_ms=duration_ms,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        output_truncated=False,
        user_cpu_ms=1000,
        system_cpu_ms=200,
        max_rss_kib=1024,
    )


def obs(**kwargs):
    defaults = dict(
        execution_id="exec-1",
        tool_name="exec",
        command="python -m pytest -q",
        command_digest="sha256-abcdef",
        repo_fingerprint="github.com/acme/foo",
        run_id="run-1",
        sequence_no=0,
        duration_sec=5.0,
        status_code="ok",
        exit_code=0,
        complete=True,
        collection_quality=CollectionQuality.VALID,
        coverage_ratio=1.0,
        cpu_time_sec=5.0,
        cpu_utilization_avg_cores=1.0,
        rss_peak_bytes=1024**2,
        memory_rss_bytes_after=1024**2,
        trusted=True,
        created_at=utcnow(),
    )
    defaults.update(kwargs)
    return ToolObservation(**defaults)


# ── Schema ──────────────────────────────────────────────────────────────

def test_duration_buckets():
    assert duration_bucket(None) is None
    assert duration_bucket(0.5) == "short"
    assert duration_bucket(1.0) == "short"
    assert duration_bucket(5.0) == "medium"
    assert duration_bucket(30.0) == "medium"
    assert duration_bucket(31.0) == "long"


def test_span_end_to_observation_valid():
    record = span_end("exec-abc", status="ok", exit_code=0, duration_sec=5.0)
    result = span_end_to_observation(record)
    assert result is not None
    assert result.execution_id == "exec-abc"
    assert result.collection_quality == CollectionQuality.VALID
    assert result.complete is True
    assert result.duration_sec == 5.0
    assert result.latency_bucket == "medium"
    assert result.cpu_utilization_avg_cores == 1.5
    assert result.rss_peak_bytes == 1024**2


def test_span_end_to_observation_ignores_non_tool_and_missing_id():
    assert span_end_to_observation({"record_type": "span_start"}) is None
    bad = span_end("exec-abc")
    bad["kind"] = "llm"
    assert span_end_to_observation(bad) is None
    missing = span_end("exec-abc")
    missing["execution"]["execution_id"] = None
    assert span_end_to_observation(missing) is None


def test_span_end_timeout_degrades_quality():
    record = span_end("exec-t", status="timeout", exit_code=124, duration_sec=10.0)
    result = span_end_to_observation(record)
    assert result is not None
    assert result.status_code == "timeout"
    assert result.complete is True


# ── Join (execution_id exact) ───────────────────────────────────────────

def test_join_is_exact_and_100_percent():
    spans = [span_end("exec-1", command="pytest -q"), span_end("exec-2", command="sleep 2")]
    bridges = [bridge_record("exec-1"), bridge_record("exec-2")]
    result = join_trace_and_bridge(spans, bridges)
    assert result.join_rate == 1.0
    assert len(result.joined) == 2
    assert len(result.unmatched_spans) == 0
    assert len(result.unmatched_bridges) == 0
    # Bridge exit_code is authoritative.
    assert result.joined[0].exit_code == 0


def test_join_uses_bridge_exit_code_over_span():
    spans = [span_end("exec-1", status="ok", exit_code=0)]
    bridges = [bridge_record("exec-1", exit_code=3)]
    result = join_trace_and_bridge(spans, bridges)
    assert result.join_rate == 1.0
    assert result.joined[0].exit_code == 3
    assert result.joined[0].trusted is False


def test_join_reports_unmatched_sides():
    spans = [span_end("exec-1"), span_end("exec-orphan")]
    bridges = [bridge_record("exec-1"), bridge_record("exec-extra")]
    result = join_trace_and_bridge(spans, bridges)
    assert result.join_rate == 0.5
    assert len(result.joined) == 1
    assert [s.execution_id for s in result.unmatched_spans] == ["exec-orphan"]
    assert [b.execution_id for b in result.unmatched_bridges] == ["exec-extra"]


def test_join_ignores_non_span_records():
    spans = [
        {"record_type": "trace_metadata", "schema_version": 6},
        span_end("exec-1"),
    ]
    bridges = [bridge_record("exec-1")]
    result = join_trace_and_bridge(spans, bridges)
    assert result.join_rate == 1.0
    assert len(result.joined) == 1


def test_envelope_execution_id_roundtrip():
    """The envelope format the plugin emits must be parseable by the join."""
    # This mirrors buildSandboxExecEnvelope output: "__CBX_EXEC_1__<json>\n<cmd>"
    execution_id = "exec-1234"
    envelope = f'__CBX_EXEC_1__{{"v":1,"execution_id":"{execution_id}"}}\npython -m pytest -q'
    # The span carries the execution_id from the plugin; the bridge record
    # carries the same one after parsing the envelope.  Join must be exact.
    spans = [span_end(execution_id, command="python -m pytest -q")]
    bridges = [bridge_record(execution_id)]
    result = join_trace_and_bridge(spans, bridges)
    assert result.join_rate == 1.0
    assert result.joined[0].execution_id == execution_id


# ── Validator / signature / dedup ───────────────────────────────────────

def test_hmac_signature_roundtrip():
    observation = obs()
    signed = sign_observation(observation, "secret")
    assert verify_signature(observation, "secret", signed) is True
    assert verify_signature(observation, "wrong-secret", signed) is False
    assert verify_signature(observation, "secret", "deadbeef") is False


def test_validator_rejects_each_gate():
    validator = ObservationValidator(ingest_secret="secret")
    signed = sign_observation(obs(), "secret")
    assert validator.validate(obs(), signed).valid

    bad_schema = obs(schema_version=99)
    assert not validator.validate(bad_schema, sign_observation(bad_schema, "secret")).valid

    bad_identity = obs(tool_name="")
    assert not validator.validate(bad_identity, sign_observation(bad_identity, "secret")).valid

    bad_sig = obs()
    assert not validator.validate(bad_sig, "not-a-signature").valid

    bad_quality = obs(complete=False)
    assert not validator.validate(bad_quality, sign_observation(bad_quality, "secret")).valid

    censored = obs(exit_code=1)
    assert not validator.validate(censored, sign_observation(censored, "secret")).valid


def test_classify_observations_partitions():
    validator = ObservationValidator(ingest_secret="secret")
    trusted = obs(execution_id="exec-ok")
    duplicate = obs(execution_id="exec-ok")
    rejected = obs(execution_id="exec-bad", exit_code=1)
    sigs = {
        dedup_key(trusted): sign_observation(trusted, "secret"),
        dedup_key(rejected): sign_observation(rejected, "secret"),
    }
    report = classify_observations([trusted, duplicate, rejected], validator, sigs)
    assert [o.execution_id for o in report.trusted] == ["exec-ok"]
    assert [o.execution_id for o in report.duplicates] == ["exec-ok"]
    assert [o.execution_id for o, _ in report.rejected] == ["exec-bad"]


# ── Dataset ─────────────────────────────────────────────────────────────

def test_read_and_build_dataset(tmp_path):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    (trace_dir / "run-1.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [
            {"record_type": "trace_metadata", "schema_version": 6},
            span_end("exec-1", command="pytest -q", duration_sec=3.0),
            span_end("exec-2", command="sleep 5", duration_sec=5.0),
        ]),
        encoding="utf-8",
    )
    bridge_path = tmp_path / "tool-bridge.jsonl"
    bridge_path.write_text(
        "\n".join(json.dumps(b.model_dump(mode="json")) for b in [
            bridge_record("exec-1"),
            bridge_record("exec-2"),
        ]),
        encoding="utf-8",
    )
    assert len(read_trace_jsonl(trace_dir / "run-1.jsonl")) == 3
    assert len(read_bridge_jsonl(bridge_path)) == 2
    joined, trusted = build_joined_dataset(trace_dir, bridge_path)
    assert joined.join_rate == 1.0
    assert len(joined.joined) == 2
    assert len(trusted) == 2


def test_export_dataset_writes_train_eval(tmp_path):
    observations = [
        obs(execution_id=f"exec-{i}", command_digest=f"sha256-{i % 4}", duration_sec=float(i % 5 + 1))
        for i in range(24)
    ]
    counts = export_dataset(observations, tmp_path, write_parquet=False, split="command")
    assert (tmp_path / "train.jsonl").exists()
    assert (tmp_path / "eval.jsonl").exists()
    assert counts["train"] + counts["eval"] == 24
    assert counts["train"] > 0 and counts["eval"] > 0

    def digests(path):
        with path.open(encoding="utf-8") as handle:
            return {json.loads(line)["command_digest"] for line in handle}

    train_d, eval_d = digests(tmp_path / "train.jsonl"), digests(tmp_path / "eval.jsonl")
    # Command-disjoint split: no command digest leaks across the boundary.
    assert train_d.isdisjoint(eval_d)


def test_command_disjoint_split_no_leakage():
    observations = [
        obs(execution_id=f"exec-{i}", command_digest=f"sha256-{i % 6}", duration_sec=float(i % 5 + 1))
        for i in range(30)
    ]
    from clawbox.tuning.dataset import command_disjoint_split

    train, eval_ = command_disjoint_split(observations, train_frac=0.7, seed=3)
    train_d = {o.command_digest for o in train}
    eval_d = {o.command_digest for o in eval_}
    assert train_d.isdisjoint(eval_d)
    assert len(train) + len(eval_) == 30


# ── Estimators ──────────────────────────────────────────────────────────

def test_latency_estimator_per_command_and_fallback():
    data = [obs(execution_id=f"e-{i}", command="pytest -q", command_digest="sha256-aa",
                duration_sec=float(i + 1)) for i in range(10)]
    estimator = LatencyEstimator(data)
    known = estimator.predict(obs(command="pytest -q", command_digest="sha256-aa"))
    assert known.source == "per-command"
    assert known.sample_count == 10
    assert known.p90_sec >= known.p50_sec
    unknown = estimator.predict(obs(command="brand-new", command_digest="sha256-zz"))
    assert unknown.source == "global-default"
    assert unknown.sample_count == 0


def test_bucket_classifier_and_memory():
    data = [obs(execution_id=f"e-{i}", command="fast", command_digest="sha256-fast",
                duration_sec=0.5, rss_peak_bytes=512 * 1024) for i in range(5)]
    data += [obs(execution_id=f"m-{i}", command="slow", command_digest="sha256-slow",
                 duration_sec=60.0, rss_peak_bytes=2048 * 1024) for i in range(5)]
    buckets = LatencyBucketClassifier(data)
    assert buckets.predict(obs(command="fast", command_digest="sha256-fast")) == "short"
    assert buckets.predict(obs(command="slow", command_digest="sha256-slow")) == "long"
    mem = MemoryEstimator(data)
    predicted, sample_count, source = mem.predict(obs(command="fast", command_digest="sha256-fast"))
    assert sample_count == 5
    assert source == "per-command"
    assert predicted >= 512 * 1024


def test_evaluate_predictions_and_cross_validate():
    data = [
        obs(execution_id=f"e-{i}", command=f"cmd-{i % 6}", command_digest=f"sha256-{i % 6}",
            duration_sec=float(i % 5 + 1), rss_peak_bytes=1024 * 1024)
        for i in range(30)
    ]
    estimator = LatencyEstimator(data)
    buckets = LatencyBucketClassifier(data)
    metrics = evaluate_predictions(data, estimator, buckets)
    assert metrics.n == 30
    assert metrics.mae_sec >= 0
    assert 0 <= metrics.calibration_p90 <= 1
    result = cross_validate(data, train_frac=0.8, seed=7)
    assert result["train_n"] > 0 and result["eval_n"] > 0
    assert result["cold_start"] is True
    assert "mae_sec" in result and "bucket_accuracy" in result


# ── KB snapshot / generation / rollback ─────────────────────────────────

def test_kb_builder_generation_and_rollback():
    builder = KnowledgeBaseBuilder()
    builder.add_many([obs(execution_id="e-1", command="pytest -q", command_digest="sha256-aa")])
    first = builder.build()
    builder.add_many([obs(execution_id="e-2", command="pytest -q", command_digest="sha256-aa")])
    second = builder.build()
    assert first.metadata.generation == 1
    assert second.metadata.generation == 2
    assert second.metadata.input_count == 2
    assert second.metadata.input_digest != first.metadata.input_digest
    # Snapshot round-trip preserves the model.
    restored = KnowledgeBase.from_snapshot(second.snapshot())
    probe = obs(command="pytest -q", command_digest="sha256-aa")
    original_pred = second.predict(probe)
    restored_pred = restored.predict(probe)
    assert original_pred.latency_p90_sec == restored_pred.latency_p90_sec
    # Rollback drops the latest generation.
    assert builder.rollback() is first
    assert builder.rollback() is None


def test_kb_predict_returns_prediction():
    data = [
        obs(execution_id=f"e-{i}", command="pytest -q", command_digest="sha256-aa",
            duration_sec=2.0, cpu_utilization_avg_cores=1.0, rss_peak_bytes=1024 * 1024)
        for i in range(10)
    ]
    builder = KnowledgeBaseBuilder()
    builder.add_many(data)
    kb = builder.build()
    prediction = kb.predict(obs(command="pytest -q", command_digest="sha256-aa"))
    assert prediction.time_bucket in ("short", "medium", "long")
    assert prediction.memory_p90_bytes >= 1024 * 1024
    assert prediction.sample_count == 10
    assert prediction.confidence == 1.0


# ── Ablation ────────────────────────────────────────────────────────────

def _ablation_data():
    """10 heterogeneous commands x 6 samples each (deterministic jitter)."""
    base = [1.0, 2.0, 3.0, 1.5, 2.5, 3.5, 1.2, 2.2, 3.2, 4.0]
    jitter = [0.0, 0.1, 0.2, -0.1, 0.3, 0.05]
    data = []
    index = 0
    for cmd, duration in enumerate(base):
        for offset in jitter:
            data.append(obs(
                execution_id=f"e-{index}",
                command=f"cmd-{cmd}",
                command_digest=f"sha256-{cmd}",
                duration_sec=max(0.05, duration + offset),
                rss_peak_bytes=512 * 1024,
                cpu_utilization_avg_cores=0.5,
            ))
            index += 1
    return data


def test_ablation_cold_start_learning_beats_fixed_profile():
    from clawbox.tuning.ablation import FixedProfilePredictor, run_ablation

    result = run_ablation(
        _ablation_data(),
        train_frac=0.7,
        seed=7,
        fixed_profile=FixedProfilePredictor(latency_p90_sec=60.0),
    )
    cold = result.cold_start
    assert cold.n_train > 0 and cold.n_eval > 0
    # Any learning (global-only or KB) beats the fixed 60s profile on MAE.
    assert cold.kb["mae_sec"] <= cold.baseline["mae_sec"]
    assert cold.global_only["mae_sec"] <= cold.baseline["mae_sec"]
    # Cold start: KB and global-only both fall back to the global distribution.
    assert cold.kb["mae_sec"] <= cold.global_only["mae_sec"] + 1e-6
    # Fixed 60s profile pays a much larger over-allocation than the KB.
    assert cold.kb["mean_over_allocation_pct"] < cold.baseline["mean_over_allocation_pct"]
    assert cold.kb_mae_delta_pct() > 0


def test_ablation_known_command_kb_beats_global_only():
    from clawbox.tuning.ablation import run_ablation

    result = run_ablation(_ablation_data(), train_frac=0.8, seed=11)
    known = result.known_command
    assert known.n_train > 0 and known.n_eval > 0
    # Known-command scenario: the per-command KB history beats a single global
    # distribution on MAE (per-command median is closer to each command).
    assert known.kb["mae_sec"] <= known.global_only["mae_sec"] + 1e-6
    # And it uses far less headroom than the fixed 60s profile.
    assert known.kb["mean_over_allocation_pct"] < known.baseline["mean_over_allocation_pct"]
    summary = result.summary()
    assert summary["n_total"] == 60
    assert summary["known_command"]["kb_mae_delta_pct"] >= summary["cold_start"]["kb_mae_delta_pct"]
