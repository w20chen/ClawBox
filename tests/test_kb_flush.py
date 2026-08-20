"""Cross-check the runtime kb-flush.py against the canonical tuning pipeline.

The runtime flush script must ship observations the control-plane projector
accepts: same normalized fields, same HMAC canonical payload.  This test feeds
the same synthetic span/bridge JSONL through both paths and asserts they agree.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from clawbox.tuning.join import join_trace_and_bridge
from clawbox.tuning.schema import BridgeRecord, span_end_to_observation
from clawbox.tuning.validate import sign_observation

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_kb_flush():
    spec = importlib.util.spec_from_file_location("kb_flush", SCRIPTS / "kb-flush.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def span_end(execution_id, *, command="python -m pytest -q", duration_sec=5.0,
             exit_code=0, coverage=1.0, seq=0, repo="github.com/acme/foo"):
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
        "name": "exec",
        "wall_time_ns": str(wall_ns),
        "monotonic_time_ns": "0",
        "duration_ns": str(int(duration_sec * 1e9)),
        "duration_sec": str(duration_sec),
        "repo": repo,
        "status": {"code": "ok", "message": None},
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
            "cpu_time_s": duration_sec * 1.5,
            "cpu_utilization_avg_cores": 1.5,
            "rss_peak_bytes": 1024**2,
            "memory_rss_bytes_after": 1024**2,
            "sampling_interval_ms": 100,
        },
    }


def bridge(execution_id, *, duration_ms=5000, exit_code=0, stdout_bytes=128,
           stderr_bytes=0):
    return {
        "timestamp": "2026-08-19T00:00:00Z",
        "cell_id": None,
        "task_id": None,
        "execution_id": execution_id,
        "execution_source": "runtime-envelope",
        "command_sha256": f"sha256-{execution_id[-8:]}",
        "command_bytes": 64,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "timed_out": False,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "output_truncated": False,
    }


def test_join_matches_canonical_pipeline(tmp_path):
    kb_flush = load_kb_flush()
    span_records = [span_end("exec-0001"), span_end("exec-0002", exit_code=1)]
    bridge_records = [bridge("exec-0001"), bridge("exec-0002", exit_code=1)]

    # Canonical path (clawbox.tuning).
    canonical = join_trace_and_bridge(span_records, [BridgeRecord.model_validate(b) for b in bridge_records])
    canonical_ids = {obs.execution_id for obs in canonical.joined}
    assert canonical.join_rate == 1.0

    # Runtime script path.
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    (trace_dir / "run.jsonl").write_text(
        "\n".join(__import__("json").dumps(r) for r in span_records) + "\n", encoding="utf-8"
    )
    (trace_dir / "tool-bridge.jsonl").write_text(
        "\n".join(__import__("json").dumps(b) for b in bridge_records) + "\n", encoding="utf-8"
    )
    joined = kb_flush.join_observations(trace_dir, trace_dir / "tool-bridge.jsonl", repo="github.com/acme/foo")
    joined_ids = {obs["execution_id"] for obs in joined}
    assert joined_ids == canonical_ids
    assert len(joined) == 2
    by_id = {obs["execution_id"]: obs for obs in joined}
    assert by_id["exec-0002"]["exit_code"] == 1
    assert by_id["exec-0002"]["status_code"] == "error"
    assert by_id["exec-0001"]["status_code"] == "ok"


def test_cgroup_artifact_overrides_span_estimates(tmp_path):
    """The Tool-VM collector artifact must replace the span-side proxy values
    (cpu/rss/quality) before the observation is signed and trusted."""
    kb_flush = load_kb_flush()
    execution_id = "exec-0001"
    span_records = [span_end(execution_id, duration_sec=5.0)]
    bridge_records = [bridge(execution_id)]

    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    (trace_dir / "run.jsonl").write_text(
        "\n".join(__import__("json").dumps(r) for r in span_records) + "\n", encoding="utf-8"
    )
    (trace_dir / "tool-bridge.jsonl").write_text(
        "\n".join(__import__("json").dumps(b) for b in bridge_records) + "\n", encoding="utf-8"
    )
    resource_dir = trace_dir / "tool-resource"
    resource_dir.mkdir()
    (resource_dir / f"cgroup-resource-{execution_id}.json").write_text(
        __import__("json").dumps({
            "schema": "cgroup_resource_v1",
            "execution_id": execution_id,
            "source": "cgroup-v2",
            "monitor_source": "cgroup-v2",
            "attribution_source": "tool-bridge-pgid",
            "cpu_time_s": 0.123,
            "cpu_utilization_avg_cores": 0.12,
            "memory_rss_peak_bytes": 42 * 1024 * 1024,
            "sampling_interval_ms": 100,
            "sampling_point_count": 7,
            "sampling_quality": "valid",
        }) + "\n", encoding="utf-8"
    )
    joined = kb_flush.join_observations(trace_dir, trace_dir / "tool-bridge.jsonl", repo="github.com/acme/foo")
    assert len(joined) == 1
    obs = joined[0]
    assert obs["cpu_time_sec"] == 0.123  # real value, not the span's 7.5
    assert obs["rss_peak_bytes"] == 42 * 1024 * 1024  # not the span's 1 MiB
    assert obs["source"] == "cgroup-v2"
    assert obs["resource_source"] == "cgroup-v2"
    assert obs["collection_quality"] == "valid"
    assert obs["trusted"] is True
    # Signing happens over the final (real) values, so the HMAC must match
    # a canonical payload built from those same values.
    canonical = {key: obs.get(key) for key in kb_flush.SIGNED_FIELDS}
    assert kb_flush.canonical_payload(canonical) == kb_flush.canonical_payload(obs)
    assert kb_flush.sign_observation(obs, "secret") == kb_flush.sign_observation(
        {**obs, "cpu_time_sec": 0.123, "rss_peak_bytes": 42 * 1024 * 1024,
         "collection_quality": "valid"}, "secret")


def test_read_cgroup_artifacts_both_layouts(tmp_path):
    kb_flush = load_kb_flush()
    trace_dir = tmp_path / "traces"
    resource_dir = trace_dir / "tool-resource"
    resource_dir.mkdir(parents=True)
    (resource_dir / "cgroup-resource-exec-a.json").write_text(
        '{"schema":"cgroup_resource_v1","execution_id":"exec-a","source":"process-tree","cpu_time_s":1.5}\n',
        encoding="utf-8",
    )
    (trace_dir / "cgroup-resource-exec-b.json").write_text(
        '{"schema":"cgroup_resource_v1","execution_id":"exec-b","source":"process-tree","cpu_time_s":2.5}\n',
        encoding="utf-8",
    )
    (trace_dir / "cgroup-resource-bad.json").write_text("{not json\n", encoding="utf-8")
    artifacts = kb_flush.read_cgroup_artifacts(trace_dir)
    assert set(artifacts) == {"exec-a", "exec-b"}
    assert artifacts["exec-a"]["cpu_time_s"] == 1.5
    assert artifacts["exec-b"]["source"] == "process-tree"


def test_signature_matches_canonical(tmp_path):
    kb_flush = load_kb_flush()
    record = span_end("exec-0003")
    obs_dict = kb_flush.span_end_to_obs(record)
    assert obs_dict is not None
    # The runtime signature must equal the control-plane sign_observation over
    # the same observation parsed as a ToolObservation.
    canonical_obs = span_end_to_observation(record)
    assert canonical_obs is not None
    secret = "ingest-secret"
    runtime_sig = kb_flush.sign_observation(obs_dict, secret)
    canonical_sig = sign_observation(canonical_obs, secret)
    assert runtime_sig == canonical_sig


def test_unmatched_span_is_dropped(tmp_path):
    kb_flush = load_kb_flush()
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    # Span has no bridge record -> not joinable -> dropped, never trusted.
    (trace_dir / "run.jsonl").write_text(__import__("json").dumps(span_end("orphan-0001")) + "\n", encoding="utf-8")
    (trace_dir / "tool-bridge.jsonl").write_text("", encoding="utf-8")
    joined = kb_flush.join_observations(trace_dir, trace_dir / "tool-bridge.jsonl", repo="github.com/acme/foo")
    assert joined == []
