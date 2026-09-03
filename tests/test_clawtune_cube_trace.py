from __future__ import annotations

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
    )

    joined, trusted = build_joined_dataset(
        tmp_path / "traces", tmp_path / "tool-bridge.jsonl",
    )
    assert joined.join_rate == 1.0
    assert len(trusted) == 1
    assert trusted[0].execution_id == execution_id
    assert trusted[0].tool_name == "cube_shell"
    assert trusted[0].duration_sec == 1.25
    assert trusted[0].rss_peak_bytes is None
    assert execution_id == "call-123"

    bridge = json.loads((tmp_path / "tool-bridge.jsonl").read_text().strip())
    assert bridge["command_sha256"] == trusted[0].command_digest
