from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from clawbox.replay._numa_exec import parse_cpu_set
from clawbox.replay.engine import ReplayEngine, SnapshotPolicy
from clawbox.replay.guest import RuntimeAgentState, VsockRuntimeAgentClient
from clawbox.replay.inference import TraceReplayInferenceProvider
from clawbox.replay.latency import LatencyObservation, LinearLatencyPredictor
from clawbox.replay.lifecycle import FirecrackerConfig, FirecrackerLifecycle, LocalCommandExecutor, SimulatedLifecycle
from clawbox.replay.trace import ReplayAction, load_trace


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_load_v4_actions_and_extract_shell_command(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_jsonl(trace, [
        {
            "type": "action", "action_type": "llm_call", "action_id": "llm-1",
            "iteration": 0, "ts_start": 10.0, "ts_end": 15.0,
            "data": {"messages_in": [{"role": "user", "content": "hello"}],
                     "raw_response": {"content": "world"}, "llm_latency_ms": 4200},
        },
        {
            "type": "action", "action_type": "tool_exec", "action_id": "tool-1",
            "iteration": 0, "ts_start": 15.0, "ts_end": 16.0,
            "data": {"tool_name": "exec", "args": {"command": "printf ok"},
                     "exit_code": 0, "result": "ok"},
        },
    ])
    actions = load_trace(trace)
    assert [action.kind for action in actions] == ["llm", "tool"]
    assert actions[0].duration_s == pytest.approx(4.2)
    assert actions[1].shell_command() == "printf ok"
    assert actions[1].expected_exit_code == 0


def test_load_clawtune_v6_spans(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_jsonl(trace, [
        {"schema_version": 6, "record_type": "span_start", "trace_id": "r",
         "span_id": "l", "sequence_no": 1, "kind": "llm", "name": "model",
         "wall_time_ns": "1000000000", "input": {"messages": ["hello"]}},
        {"schema_version": 6, "record_type": "span_end", "trace_id": "r",
         "span_id": "l", "sequence_no": 1, "kind": "llm", "name": "model",
         "wall_time_ns": "3000000000", "duration_ns": "2000000000",
         "output": {"content": "response"}},
        {"schema_version": 6, "record_type": "span_start", "trace_id": "r",
         "span_id": "t", "sequence_no": 2, "kind": "tool", "name": "exec",
         "wall_time_ns": "3000000000", "input": {"requested_args": '{"command":"true"}'}},
        {"schema_version": 6, "record_type": "span_end", "trace_id": "r",
         "span_id": "t", "sequence_no": 2, "kind": "tool", "name": "exec",
         "wall_time_ns": "3100000000", "duration_ns": "100000000",
         "output": {"exit_code": 0, "result": ""}},
    ])
    actions = load_trace(trace)
    assert actions[0].duration_s == pytest.approx(2.0)
    assert actions[1].shell_command() == "true"


def test_incomplete_v6_span_fails_closed(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_jsonl(trace, [{
        "record_type": "span_start", "trace_id": "r", "span_id": "x",
        "kind": "llm", "wall_time_ns": "1", "input": {},
    }])
    with pytest.raises(ValueError, match="incomplete"):
        load_trace(trace)


def test_latency_prediction_does_not_read_current_response() -> None:
    predictor = LinearLatencyPredictor.fit([
        LatencyObservation(100, 10, 2),
        LatencyObservation(200, 2000, 4),
        LatencyObservation(300, 20, 6),
    ])
    short = ReplayAction("llm", "a", 1, 0, 1, "m", "x" * 250, "short")
    long = ReplayAction("llm", "b", 1, 0, 1, "m", "x" * 250, "y" * 100000)
    assert predictor.predict(short) == predictor.predict(long)
    assert predictor.seconds_per_input_char > 0


def test_engine_reexecutes_tool_around_snapshot(tmp_path: Path) -> None:
    output = tmp_path / "executed.txt"
    actions = [
        ReplayAction("llm", "l", 1, 0, 0.03, "m", "large request", "ignored"),
        ReplayAction(
            "tool", "t", 2, 30, 0.1, "exec",
            {"command": f'python -c "from pathlib import Path; Path(r\'{output}\').write_text(\'ran\')"'},
            "", 0,
        ),
    ]
    lifecycle = SimulatedLifecycle()
    predictor = LinearLatencyPredictor(
        intercept_s=0.03, seconds_per_input_char=0, expected_output_chars=0, sample_count=1,
    )
    events: list[dict] = []
    summary = ReplayEngine(
        lifecycle=lifecycle,
        executor=LocalCommandExecutor(),
        predictor=predictor,
        policy=SnapshotPolicy(
            min_predicted_llm_s=0.02, estimated_snapshot_s=0,
            estimated_restore_s=0, safety_margin_s=0,
        ),
        mode="snapshot",
        sleep_scale=1,
        event_sink=events.append,
    ).run(actions)
    assert output.read_text() == "ran"
    assert summary.snapshots == 1
    assert summary.exit_mismatches == 0
    assert [event["event"] for event in events].index("sandbox_evicted") < [
        event["event"] for event in events
    ].index("sandbox_restored")


class _StatefulRuntimeAgent:
    def __init__(self) -> None:
        self.boot_nonce = 42
        self.turn = 0
        self.tool_count = 0
        self.inflight: str | None = None
        self.calls: list[str] = []

    def _state(self) -> RuntimeAgentState:
        return RuntimeAgentState(
            self.boot_nonce, self.turn, self.tool_count, self.inflight,
            0, "replay-gpu-unbounded", 4096, 128 * 1024 * 1024,
        )

    def wait_ready(self, timeout_s: float) -> float:
        self.calls.append("ready")
        return 0.0

    def begin_llm(self, request_id: str, predicted_s: float,
                  metadata: dict) -> RuntimeAgentState:
        assert metadata["kv_bytes"] == 4096
        self.calls.append(f"begin:{request_id}")
        self.inflight = request_id
        return self._state()

    def assert_inflight(self, request_id: str,
                        expected_boot_nonce: int) -> RuntimeAgentState:
        self.calls.append(f"assert:{request_id}")
        assert expected_boot_nonce == self.boot_nonce
        assert request_id == self.inflight
        return self._state()

    def complete_llm(self, request_id: str) -> RuntimeAgentState:
        self.calls.append(f"complete:{request_id}")
        assert request_id == self.inflight
        self.inflight = None
        self.turn += 1
        return self._state()

    def tool_completed(self, action_id: str, exit_code: int) -> RuntimeAgentState:
        self.calls.append(f"tool:{action_id}:{exit_code}")
        self.tool_count += 1
        return self._state()


def test_engine_preserves_runtime_protocol_across_snapshot() -> None:
    agent = _StatefulRuntimeAgent()
    actions = [
        ReplayAction("llm", "llm-1", 1, 0, 0.03, "model", "prompt", "response"),
        ReplayAction("tool", "tool-1", 2, 30, 0, "exec", {"command": "exit 0"}, "", 0),
    ]
    summary = ReplayEngine(
        lifecycle=SimulatedLifecycle(), executor=LocalCommandExecutor(),
        predictor=LinearLatencyPredictor(
            intercept_s=0.03, seconds_per_input_char=0,
            expected_output_chars=0, sample_count=1,
        ),
        policy=SnapshotPolicy(
            min_predicted_llm_s=0.02, estimated_snapshot_s=0,
            estimated_restore_s=0, safety_margin_s=0,
        ), mode="snapshot",
        inference_provider=TraceReplayInferenceProvider(
            time_scale=1, simulated_kv_bytes=4096,
        ),
        runtime_agent=agent,
    ).run(actions)
    assert summary.snapshots == 1
    assert agent.turn == 1
    assert agent.tool_count == 1
    assert agent.calls == [
        "ready", "begin:llm-1", "ready", "assert:llm-1",
        "complete:llm-1", "tool:tool-1:0",
    ]


def test_engine_reclaims_runtime_and_tool_sandboxes() -> None:
    runtime_agent = _StatefulRuntimeAgent()
    tool_agent = _StatefulRuntimeAgent()
    events: list[dict] = []
    summary = ReplayEngine(
        lifecycle=SimulatedLifecycle(),
        tool_lifecycle=SimulatedLifecycle(),
        executor=LocalCommandExecutor(),
        predictor=LinearLatencyPredictor(
            intercept_s=0.02, seconds_per_input_char=0,
            expected_output_chars=0, sample_count=1,
        ),
        policy=SnapshotPolicy(0.01, 0, 0, 0, 0),
        tool_policy=SnapshotPolicy(0.015, 0, 0, 0, 0),
        mode="snapshot",
        inference_provider=TraceReplayInferenceProvider(
            time_scale=1, simulated_kv_bytes=4096,
        ),
        runtime_agent=runtime_agent,
        tool_runtime_agent=tool_agent,
        event_sink=events.append,
    ).run([
        ReplayAction("llm", "llm-1", 1, 0, 0.02, "model", "prompt", "response"),
        ReplayAction("tool", "tool-1", 2, 0.02, 0, "exec", {"command": "exit 0"}, "", 0),
    ])
    assert summary.snapshots == 1
    assert summary.tool_snapshots == 1
    assert runtime_agent.turn == tool_agent.turn == 1
    assert runtime_agent.tool_count == tool_agent.tool_count == 1
    names = [event["event"] for event in events]
    assert names.index("tool_sandbox_evicted") < names.index("tool_sandbox_restored")
    assert names.index("tool_sandbox_restored") < names.index("tool_end")


def test_guest_protocol_rejects_unescaped_tokens(tmp_path: Path) -> None:
    client = VsockRuntimeAgentClient(tmp_path / "unused.sock")
    with pytest.raises(ValueError, match="protocol token"):
        client.begin_llm("request with spaces", 1.0, {})
    with pytest.raises(ValueError, match="protocol token"):
        client.begin_llm("ok", 1.0, {"gpu_id": 'gpu\"bad'})


def test_trace_inference_provider_exposes_future_gpu_metadata() -> None:
    action = ReplayAction("llm", "llm-1", 1, 0, 0, "model", "x", "y")
    wait = TraceReplayInferenceProvider(
        time_scale=0.25, simulated_gpu_id="gpu-sim-0", simulated_kv_bytes=1234,
    ).begin(action, 9.0)
    assert wait.estimated_wait_s == pytest.approx(2.25)
    result = wait.wait_ready()
    assert result.request_id == "llm-1"
    assert result.predicted_s == 9.0
    assert result.simulated_s == 0.0
    assert result.metadata == {
        "provider": "trace-replay", "gpu_id": "gpu-sim-0",
        "kv_cache_handle": None, "kv_bytes": 1234,
    }


def test_engine_detects_replay_divergence() -> None:
    action = ReplayAction("tool", "t", 1, 0, 0, "exec", {"command": "exit 7"}, "", 0)
    with pytest.raises(RuntimeError, match="exit mismatch"):
        ReplayEngine(
            lifecycle=SimulatedLifecycle(),
            executor=LocalCommandExecutor(),
            predictor=LinearLatencyPredictor.from_actions([]),
            policy=SnapshotPolicy(), mode="resident", sleep_scale=0,
        ).run([action])


def test_firecracker_snapshot_paths_alternate(tmp_path: Path) -> None:
    config = FirecrackerConfig(
        binary=tmp_path / "firecracker", api_socket=tmp_path / "api.sock",
        kernel_image=tmp_path / "kernel", rootfs=tmp_path / "rootfs",
        snapshot_state=tmp_path / "state", snapshot_memory=tmp_path / "memory",
    )
    lifecycle = FirecrackerLifecycle(config)
    assert lifecycle._next_snapshot_paths() == (tmp_path / "state", tmp_path / "memory")
    lifecycle._snapshot_state_path = tmp_path / "state"
    lifecycle._snapshot_memory_path = tmp_path / "memory"
    assert lifecycle._next_snapshot_paths() == (
        tmp_path / "state.next", tmp_path / "memory.next",
    )


@pytest.mark.skipif(os.name == "nt", reason="generated commands target the Linux Firecracker guest")
def test_common_file_tools_are_replayable(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "file.txt"
    executor = LocalCommandExecutor()
    write = ReplayAction("tool", "w", 1, 0, 0, "write", {"path": str(path), "content": "old"})
    edit = ReplayAction(
        "tool", "e", 2, 0, 0, "edit",
        {"file_path": str(path), "old_string": "old", "new_string": "new"},
    )
    read = ReplayAction("tool", "r", 3, 0, 0, "read", {"path": str(path)})
    assert executor.execute(write.shell_command(), 10).exit_code == 0
    assert executor.execute(edit.shell_command(), 10).exit_code == 0
    result = executor.execute(read.shell_command(), 10)
    assert result.exit_code == 0
    assert result.stdout == "new"


def test_multi_edit_tool_is_replayable() -> None:
    action = ReplayAction(
        "tool", "e", 1, 0, 0, "edit",
        {"path": "file.py", "edits": [
            {"oldText": "one", "newText": "1"},
            {"oldText": "two", "newText": "2"},
        ]},
    )
    command = action.shell_command()
    assert "python3 -c" in command
    assert "file.py" in command


def test_parse_cpu_set() -> None:
    assert parse_cpu_set("0-2,7,9-10") == {0, 1, 2, 7, 9, 10}
    with pytest.raises(ValueError):
        parse_cpu_set("4-2")
