from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from clawbox.clawtune_integration import seed_directory, source_revision
from clawbox.replay.trace import load_trace
from clawbox.tuning.clawtune import predict_native_call_load


def script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_fetches_main_without_touching_checkout(tmp_path):
    def git(root, *args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    git(remote, "config", "user.email", "test@example.invalid")
    git(remote, "config", "user.name", "Test")
    (remote / "value").write_text("first")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "first")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    (checkout / "value").write_text("local edits")
    (remote / "value").write_text("latest")
    git(remote, "commit", "-am", "latest")
    output = tmp_path / "export"
    revision = script("prepare-clawtune.py").prepare(checkout, output)
    assert revision == git(remote, "rev-parse", "main")
    assert source_revision(output) == revision
    assert (output / "value").read_text() == "latest"
    assert (checkout / "value").read_text() == "local edits"
    with pytest.raises(FileExistsError):
        script("prepare-clawtune.py").prepare(checkout, output)


def test_native_state_isolated_resumable_and_seed_unchanged(tmp_path):
    from clawtune_kb import FILES, StateStore
    seed = seed_directory()
    before = {p.name: p.read_bytes() for p in seed.iterdir() if p.is_file()}
    initialize = script("initialize-clawtune-state.py").initialize
    first, second = tmp_path / "first", tmp_path / "second"
    initialize(first, "runtime-a", seed)
    initialize(second, "runtime-b", seed)
    initialize(first, "runtime-a", seed)
    with pytest.raises(ValueError, match="another Runtime"):
        initialize(first, "runtime-b", seed)
    with StateStore(first):
        assert all((first / name).is_file() for name in FILES)
    assert before == {p.name: p.read_bytes() for p in seed.iterdir() if p.is_file()}


def test_main_native_model_recording_is_replayable(tmp_path):
    from clawtune_sidecar.trace import AgentTestBenchTraceWriter
    from clawtune_sidecar.contracts.models import ModelEvent
    common = dict(schema_version="clawtune.v1", event_id="event", plugin_version="test",
                  run_id="run", session_id="session", session_key=None, agent_id="agent",
                  gateway_id="gateway", runtime_id="runtime", call_id="model-1",
                  provider="test", model="test", outcome="completed")
    messages = [{"role": "user", "content": "hello"}]
    writer = AgentTestBenchTraceWriter(tmp_path)
    try:
        writer.record_model(ModelEvent(**common, event_type="model_call_started",
            occurred_at="2026-09-13T00:00:00Z", raw_input=messages))
        writer.record_model(ModelEvent(**common, event_type="model_call_ended",
            occurred_at="2026-09-13T00:00:01Z", duration_ms=1000,
            raw_output={"role": "assistant", "content": "hello"}))
        assert writer.flush()
    finally:
        writer.close()
    actions = load_trace(next(tmp_path.glob("*.jsonl")))
    assert len(actions) == 1 and actions[0].duration_s == 1
    assert actions[0].input == messages


def test_canonical_prediction_does_not_invent_peak_cpu_or_rss():
    from tool_resource.runtime_kb import CompletedCall, RuntimeToolResourceKB, ToolCallQuery
    kb = RuntimeToolResourceKB.fit_public([CompletedCall(
        "repo", "exec", "true", 0, 2, cpu_time_seconds=1, cpu_time_eligible=True,
        peak_memory_mb=100, peak_memory_mb_eligible=True, ambient_before_mb=0,
    )])
    prediction = predict_native_call_load(kb, ToolCallQuery("repo", "exec", "true", 3))
    assert prediction.targets["cpu_avg_cores"].p90 == 0.5
    assert prediction.targets["cpu_peak_cores"].status == "unavailable"
    assert prediction.targets["memory_peak_rss_bytes"].status == "unavailable"
