from __future__ import annotations

import hashlib
import json

from clawbox.experiments.clawtune_trace import ClawTuneTraceWriter
from clawbox.replay.lifecycle import CommandResult
from clawbox.tuning.dataset import build_joined_dataset


def test_cube_trace_is_exactly_joinable_and_trusted(tmp_path):
    writer = ClawTuneTraceWriter(
        tmp_path, run_id="run-1", session_id="session-1",
        repo_fingerprint="github.com/acme/repo",
    )
    execution_id = writer.record(
        "pytest -q", CommandResult(0, "ok\n", "", 1.25), execution_id="call-123",
        bridge_record={
            "execution_id": "call-123",
            "command_sha256": hashlib.sha256(b"pytest -q").hexdigest(),
            "duration_ms": 1250, "exit_code": 0, "telemetry_state": "complete",
        },
        artifacts={"cgroup_resource_v1": json.dumps({
            "schema": "cgroup_resource_v1", "execution_id": "call-123",
            "source": "cgroup-v2", "cpu_time_s": 0.5,
            "cpu_utilization_avg_cores": 0.4,
            "memory_rss_peak_bytes": 4096, "memory_rss_after_bytes": 2048,
        })},
    )

    joined, trusted = build_joined_dataset(
        tmp_path / "traces", tmp_path / "tool-bridge.jsonl",
    )
    assert joined.join_rate == 1.0
    assert len(trusted) == 1
    assert trusted[0].execution_id == execution_id
    assert trusted[0].tool_name == "exec"
    assert trusted[0].duration_sec == 1.25
    assert trusted[0].rss_peak_bytes == 4096
    assert trusted[0].cgroup is not None
    assert trusted[0].cgroup.source == "cgroup-v2"
    assert execution_id == "call-123"

    bridge = json.loads((tmp_path / "tool-bridge.jsonl").read_text().strip())
    assert bridge["execution_id"] == trusted[0].execution_id
