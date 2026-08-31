from __future__ import annotations

import json
import os
import importlib.util
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import clawbox.replay.lifecycle as lifecycle_module
from clawbox.replay._numa_exec import parse_cpu_set
from clawbox.replay.engine import ReplayEngine, SnapshotPolicy
from clawbox.replay.guest import RuntimeAgentState, VsockCommandExecutor, VsockRuntimeAgentClient
from clawbox.replay.inference import OpenAIInferenceProvider, TraceReplayInferenceProvider
from clawbox.replay.inference_service import ReplayInferenceService
from clawbox.replay.model_gateway import ModelGateway
from clawbox.replay.latency import LatencyObservation, LinearLatencyPredictor
from clawbox.replay.lifecycle import FirecrackerConfig, FirecrackerLifecycle, LocalCommandExecutor, SimulatedLifecycle
from clawbox.replay.study import _validate_firecracker_socket_paths, run_study
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


def test_vsock_tool_executor_uses_a_fresh_guest_exec_request(tmp_path: Path) -> None:
    class StubAgent(VsockRuntimeAgentClient):
        def __init__(self) -> None:
            super().__init__(tmp_path / "unused.sock")
            self.command = ""

        def _request(self, command: str, *, timeout_s: float | None = None) -> dict:
            self.command = command
            return {"ok": True, "result": {
                "exit_code": 0, "stdout_b64": "b2s=", "stderr_b64": "",
            }}

    agent = StubAgent()
    result = VsockCommandExecutor(agent).execute("printf ok", 3)
    assert agent.command == "EXEC cHJpbnRmIG9r"
    assert (result.exit_code, result.stdout, result.stderr) == (0, "ok", "")


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


def test_openai_inference_provider_calls_compatible_api() -> None:
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"usage": {"total_tokens": 7}, "choices": []}

    seen = {}

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    action = ReplayAction(
        kind="llm", action_id="llm-api", sequence_no=0, start_s=0,
        duration_s=1, name="model",
        input=[{"role": "user", "content": "hi"}],
    )
    wait = OpenAIInferenceProvider(
        base_url="https://model.example/v1/", api_key="secret",
        model="paper-model", post=post,
    ).begin(action, 2.5)
    result = wait.wait_ready()
    assert seen["url"] == "https://model.example/v1/chat/completions"
    assert seen["json"]["model"] == "paper-model"
    assert seen["json"]["messages"] == action.input
    assert result.request_id == "llm-api"
    assert result.metadata["usage"] == {"total_tokens": 7}


def test_external_inference_service_is_request_idempotent(tmp_path: Path) -> None:
    service = ReplayInferenceService(tmp_path / "inference.json", time_scale=0)
    base = service.start()
    try:
        payload = json.dumps({
            "request_id": "request-1", "session_id": "session-1",
            "content": "prompt", "recorded_latency_ms": 100,
        }).encode()
        request = urllib.request.Request(
            base + "/v1/replay/requests", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
        for _ in range(20):
            with urllib.request.urlopen(base + "/v1/replay/requests/request-1") as response:
                status = json.load(response)
            if status["ready"]:
                break
        assert status == {"request_id": "request-1", "ready": True, "result": "replayed:request-1"}
        assert json.loads((tmp_path / "inference.json").read_text())[0]["ready"] is True
    finally:
        service.close()


def test_openai_replay_gateway_preserves_tool_calls_and_is_idempotent(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_jsonl(trace, [{
        "type": "action", "action_type": "llm_call", "action_id": "model-1",
        "iteration": 0, "ts_start": 0, "ts_end": 1,
        "data": {"messages_in": [{"role": "user", "content": "run it"}],
                 "raw_response": {"content": "", "tool_calls": [{
                     "id": "call-1", "type": "function",
                     "function": {"name": "exec", "arguments": "{\"command\":\"true\"}"},
                 }]}, "llm_latency_ms": 0},
    }])
    gateway = ModelGateway(tmp_path / "store.json", mode="replay", trace=trace, time_scale=0)
    base = gateway.start("127.0.0.1", 0)
    try:
        payload = json.dumps({"model": "guest-model", "messages": [
            {"role": "user", "content": "run it"},
        ]}).encode()
        request = urllib.request.Request(
            base + "/chat/completions", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request) as response:
            first = json.load(response)
        with urllib.request.urlopen(request) as response:
            second = json.load(response)
        assert first == second
        assert first["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "exec"
        assert len(gateway.records()) == 1
        assert gateway.records()[0]["delivered"] is True
        assert gateway.records()[0]["http_attempts"] == 2
    finally:
        gateway.close()


def test_openai_gateway_gates_each_new_tool_response_once(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_jsonl(trace, [{
        "type": "action", "action_type": "llm_call", "action_id": "model-1",
        "iteration": 0, "ts_start": 0, "ts_end": 0,
        "data": {"raw_response": {"content": "", "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "exec", "arguments": "{\"command\":\"true\"}"},
        }, {
            "id": "call-2", "type": "function",
            "function": {"name": "read", "arguments": "{\"path\":\"/tmp/x\"}"},
        }]}, "llm_latency_ms": 0},
    }])
    events = []
    gateway = ModelGateway(
        tmp_path / "store.json", mode="replay", trace=trace, time_scale=0,
        on_request_started=lambda: events.append("request"),
        before_response_ready=lambda step, message: {
            "step": step, "calls": len(message.get("tool_calls") or [])
        },
    )
    payload = {"model": "ignored", "stream": True,
               "messages": [{"role": "user", "content": "go"}]}
    first = gateway.complete(payload)
    second = gateway.complete(payload)
    assert first == second
    assert events == ["request"]
    assert gateway.records()[0]["admission"] == {"step": 0, "calls": 2}


def test_gateway_request_identity_is_namespaced_and_tracks_reconnects(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_jsonl(trace, [{
        "type": "action", "action_type": "llm_call", "action_id": "model-1",
        "iteration": 0, "ts_start": 0, "ts_end": 0,
        "data": {"raw_response": {"content": "done"}, "llm_latency_ms": 0},
    }])
    payload = {"model": "ignored", "messages": [{"role": "user", "content": "go"}]}
    first = ModelGateway(
        tmp_path / "first.json", mode="replay", trace=trace, time_scale=0,
        request_namespace="session-a",
    )
    second = ModelGateway(
        tmp_path / "second.json", mode="replay", trace=trace, time_scale=0,
        request_namespace="session-b",
    )
    *_response, first_id = first._complete_with_identity(payload, http_attempt=True)
    first.mark_delivery(first_id, delivered=False)
    *_retry, retry_id = first._complete_with_identity(payload, http_attempt=True)
    first.mark_delivery(retry_id, delivered=True)
    *_other, second_id = second._complete_with_identity(payload, http_attempt=True)

    assert first_id == retry_id
    assert first_id != second_id
    record = first.records()[0]
    assert record["request_namespace"] == "session-a"
    assert record["http_attempts"] == 2
    assert record["reconnect_attempts"] == 1
    assert record["delivery_failures"] == 1
    assert record["delivered"] is True


def test_replay_gateway_rejects_recorded_request_divergence(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_jsonl(trace, [{
        "type": "action", "action_type": "llm_call", "action_id": "model-1",
        "iteration": 0, "ts_start": 0, "ts_end": 0,
        "data": {
            "raw_request": {"messages": [{"role": "user", "content": "expected"}]},
            "raw_response": {"content": "ok"}, "llm_latency_ms": 0,
        },
    }])
    gateway = ModelGateway(tmp_path / "store.json", mode="replay", trace=trace)
    with pytest.raises(ValueError, match="diverged at model step 0"):
        gateway.complete({"model": "ignored", "messages": [
            {"role": "user", "content": "different"},
        ]})


def test_streaming_model_response_can_be_exported_as_replay_trace(tmp_path: Path) -> None:
    import base64
    from clawbox.replay.model_gateway import GatewayRequest

    gateway = ModelGateway(
        tmp_path / "store.json", mode="api", upstream_base_url="https://model.invalid/v1",
        upstream_api_key="key", upstream_model="paper-model",
    )
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1",
          "type": "function", "function": {"name": "exec", "arguments": "{\"command\":"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
          "function": {"arguments": "\"true\"}"}}]}}]},
    ]
    body = "".join(f"data: {json.dumps(item)}\n\n" for item in chunks) + "data: [DONE]\n\n"
    request = GatewayRequest(
        "id", None, ready=True, status_code=200, content_type="text/event-stream",
        response_b64=base64.b64encode(body.encode()).decode(),
        started_unix_s=10.0, completed_unix_s=11.5,
    )
    gateway._requests["id"] = request
    trace = tmp_path / "recorded.jsonl"
    gateway.write_replay_trace(trace)
    action = load_trace(trace)[0]
    assert action.duration_s == 1.5
    assert action.output["tool_calls"][0]["function"] == {
        "name": "exec", "arguments": '{"command":"true"}',
    }


def test_study_runs_the_inference_memory_matrix(tmp_path: Path, monkeypatch) -> None:
    trace = tmp_path / "trace.jsonl"
    prompt = tmp_path / "prompt.txt"
    trace.write_text("{}\n", encoding="utf-8")
    prompt.write_text("run it\n", encoding="utf-8")
    config = tmp_path / "study.json"
    config.write_text(json.dumps({
        "output": "out",
        "source": {
            "trace": "trace.jsonl", "prompt": "prompt.txt",
            "runtime_rootfs": "runtime.ext4", "tool_rootfs": "tool.ext4",
        },
        "sessions": 2, "repetitions": 1,
        "inference_backends": ["replay"],
        "sizing_policies": ["fixed"],
        "fixed_control_tool_memory_mib": 2048,
        "memory_policies": ["resident", "snapshot"],
        "resources": {"runtime_memory_mib": 2048, "tool_memory_mib": 4096},
    }), encoding="utf-8")

    def fake_run(argv, **_kwargs):
        if argv[0] == "git":
            return SimpleNamespace(stdout="commit-id\n" if "rev-parse" in argv else "")
        if "prepare-openclaw-experiment.py" in str(argv[1]):
            output = Path(argv[argv.index("--output") + 1])
            output.mkdir(parents=True)
            (output / "manifest.json").write_text('{"sessions":[{}]}', encoding="utf-8")
        elif "run-openclaw-experiment.py" in str(argv[1]):
            output = Path(argv[argv.index("--output") + 1])
            output.mkdir(parents=True)
            (output / "summary.json").write_text(json.dumps({
                "sessions_completed": 2, "failures": [], "wall_s": 10,
                "throughput_sessions_per_hour": 720,
                "peak_firecracker_rss_bytes": 100,
                "mean_firecracker_rss_bytes": 80,
                "sessions": [
                    {"validation_sha256": "same-state"},
                    {"validation_sha256": "same-state"},
                ],
            }), encoding="utf-8")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr("clawbox.replay.study.subprocess.run", fake_run)
    monkeypatch.setattr(
        "clawbox.replay.study._validate_firecracker_socket_paths", lambda _arm: None,
    )
    assert run_study(config) == 0
    summary = json.loads((tmp_path / "out" / "study-summary.json").read_text())
    assert set(summary["groups"]) == {
        "replay-fixed-resident", "replay-fixed-snapshot",
        "replay-fixed2-resident", "replay-fixed2-snapshot",
    }
    assert summary["groups"]["replay-fixed-snapshot"]["sessions_completed"] == 2
    assert summary["final_state_equal"] is True
    fixed_snapshot = next((tmp_path / "out").glob("r00-replay-fixed-snapshot"))
    envelopes = json.loads((fixed_snapshot / "results"
                            / "result-envelopes.json").read_text())
    assert envelopes[0]["status"] == "succeeded"
    assert envelopes[0]["resolved_workflow"]["residency_policy"] == "llm_wait_checkpoint"
    assert envelopes[0]["metrics"]["vm_checkpoints"] == 0


def test_study_rejects_firecracker_socket_paths_over_sun_len(tmp_path: Path) -> None:
    safe = Path("/tmp/short-arm")
    _validate_firecracker_socket_paths(safe, force=True)
    too_long = Path("/tmp") / ("x" * 100)
    with pytest.raises(ValueError, match="maximum 107"):
        _validate_firecracker_socket_paths(too_long, force=True)


def test_registered_fixed2_arm_socket_path_fits_firecracker_limit() -> None:
    arm = Path(
        "/data/clawbox-paper-suite-001/runs/rec-a/c01/"
        "r00-replay-fixed2-snapshot"
    )
    _validate_firecracker_socket_paths(arm, force=True)
    assert len(os.fsencode(str(arm / "input/session-0000/runtime.sock"))) <= 107


def test_paper_suite_enforces_numa_cpu_and_memory_bounds() -> None:
    from clawbox.replay.suite import validate_numa_budget

    topology = {"node": 0, "cpulist": "0-79", "cpus": list(range(80)),
                "memory_mib": 512 * 1024}
    valid = {
        "concurrency_levels": [1, 8, 20, 40], "numa_host_reserve_mib": 32768,
        "resources": {"cpu_first": 0, "runtime_memory_mib": 2048,
                      "tool_memory_mib": 4096},
    }
    validate_numa_budget(valid, topology)
    invalid_cpu = {**valid, "concurrency_levels": [41]}
    with pytest.raises(ValueError, match="outside NUMA node"):
        validate_numa_budget(invalid_cpu, topology)
    invalid_memory = {
        **valid, "concurrency_levels": [40],
        "resources": {"cpu_first": 0, "runtime_memory_mib": 8192,
                      "tool_memory_mib": 8192},
    }
    with pytest.raises(ValueError, match="NUMA-local budget"):
        validate_numa_budget(invalid_memory, topology)


def test_paper_round_robin_cpu_placement_does_not_cap_agent_concurrency() -> None:
    from clawbox.replay.suite import validate_numa_budget

    topology = {
        "node": 0, "cpulist": "0-3", "cpus": list(range(4)),
        "memory_mib": 128 * 1024,
    }
    raw = {
        "concurrency_levels": [200],
        "numa_host_reserve_mib": 16 * 1024,
        "paper_experiment": {"dimension": "spatial"},
        "vm_pool_memory": {"hard_limit_mib": 96 * 1024},
        "resources": {
            "cpu_first": 0, "runtime_memory_mib": 2048,
            "tool_memory_mib": 4096,
        },
    }
    validate_numa_budget(raw, topology)

    raw["cpu_placement"] = "exclusive"
    with pytest.raises(ValueError, match="outside NUMA node"):
        validate_numa_budget(raw, topology)


def test_paper_suite_rejects_a_busy_numa_node(monkeypatch) -> None:
    from clawbox.replay.suite import validate_host_readiness

    ticks = iter([(1000, 800), (2000, 1500)])
    monkeypatch.setattr("clawbox.replay.suite._cpu_ticks", lambda _cpus: next(ticks))
    monkeypatch.setattr("clawbox.replay.suite._running_firecracker_pids", lambda: [])
    raw = {
        "concurrency_levels": [40], "numa_host_reserve_mib": 32768,
        "max_numa_cpu_busy_fraction": 0.1,
        "resources": {"runtime_memory_mib": 2048, "tool_memory_mib": 4096},
    }
    topology = {
        "node": 0, "cpus": list(range(80)), "free_memory_mib": 500_000,
    }
    with pytest.raises(ValueError, match="CPU busy fraction"):
        validate_host_readiness(raw, topology, sample_seconds=0)


def test_small_sample_confidence_interval_uses_student_t() -> None:
    from clawbox.replay.stats import summary_stats

    result = summary_stats([1.0, 2.0, 3.0])
    assert result["ci95_method"] == "student-t"
    assert result["degrees_of_freedom"] == 2
    assert result["ci95_half_width"] == pytest.approx(4.303 / 3**0.5)


def test_one_independent_unit_has_no_estimable_confidence_interval() -> None:
    from clawbox.replay.stats import summary_stats

    result = summary_stats([4.0])
    assert result["n"] == 1
    assert result["stdev"] is None
    assert result["ci95_half_width"] is None
    assert result["ci95_method"] == "not-estimable"


def test_macro_statistics_average_trajectories_within_independent_unit() -> None:
    from clawbox.replay.suite import SUITE_METRICS, _macro_statistics

    def group(value: float) -> dict:
        return {"statistics": {
            metric: {"mean": value if metric == "wall_s" else None}
            for metric in SUITE_METRICS
        }}

    runs = [
        {"workload": "trajectory-a", "independent_unit": "task-1", "concurrency": 1,
         "status": 0, "final_state_equal": True, "groups": {"baseline": group(2.0)}},
        {"workload": "trajectory-b", "independent_unit": "task-1", "concurrency": 1,
         "status": 0, "final_state_equal": True, "groups": {"baseline": group(4.0)}},
        {"workload": "trajectory-c", "independent_unit": "task-2", "concurrency": 1,
         "status": 0, "final_state_equal": True, "groups": {"baseline": group(9.0)}},
        {"workload": "failed", "independent_unit": "task-3", "concurrency": 1,
         "status": 1, "final_state_equal": False, "groups": {"baseline": group(100.0)}},
    ]
    result = _macro_statistics(runs, [1])["c01/baseline"]["wall_s"]
    assert result["n"] == 2
    assert result["trajectory_count"] == 3
    assert [row["mean"] for row in result["independent_unit_means"]] == [3.0, 9.0]
    assert result["mean"] == 6.0


def test_paired_contrasts_pair_repetition_and_task() -> None:
    from clawbox.replay.suite import SUITE_METRICS, _paired_contrasts

    def row(residency: str, wall: float) -> dict:
        result = {
            "workload": "trajectory-a", "independent_unit": "task-1",
            "concurrency": 8, "repetition": 0, "inference_backend": "replay",
            "sizing_policy": "fixed", "residency_policy": residency,
            "sessions_requested": 1, "sessions_completed": 1,
            "failure_count": 0, "block_final_state_equal": True,
        }
        result.update({metric: None for metric in SUITE_METRICS})
        result["wall_s"] = wall
        return result

    result = _paired_contrasts([
        row("resident", 10.0), row("llm_wait_checkpoint", 12.0),
    ])["c08/checkpoint_vs_resident/fixed"]["wall_s"]
    assert result["n"] == 1
    assert result["mean"] == 2.0
    assert result["ci95_half_width"] is None
    assert result["pairs"][0]["percent_effect"] == pytest.approx(20.0)


def test_checkpoint_suite_reserves_two_snapshot_generations(monkeypatch, tmp_path) -> None:
    from clawbox.replay.suite import validate_disk_readiness

    monkeypatch.setattr(
        "clawbox.replay.suite.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024 * 1024),
    )
    raw = {
        "concurrency_levels": [2], "memory_policies": ["snapshot"],
        "snapshot_disk_reserve_mib": 10,
        "resources": {"runtime_memory_mib": 20, "tool_memory_mib": 30},
    }
    with pytest.raises(ValueError, match="bounded concurrent snapshot generations"):
        validate_disk_readiness(raw, tmp_path)


def test_checkpoint_disk_bound_uses_resident_admission_budget(monkeypatch, tmp_path) -> None:
    from clawbox.replay.suite import validate_disk_readiness

    monkeypatch.setattr(
        "clawbox.replay.suite.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=500 * 1024 * 1024),
    )
    raw = {
        "concurrency_levels": [76], "memory_policies": ["snapshot"],
        "resident_memory_budget_mib": 160,
        "snapshot_disk_reserve_mib": 4,
        "resources": {"runtime_memory_mib": 2, "tool_memory_mib": 2},
    }
    result = validate_disk_readiness(raw, tmp_path)
    assert result["snapshot_generation_bound"] == 116
    assert result["required_mib"] == 468


def test_paper_checkpoint_disk_bound_defaults_to_whole_pair(monkeypatch, tmp_path) -> None:
    from clawbox.replay.suite import validate_disk_readiness

    monkeypatch.setattr(
        "clawbox.replay.suite.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=100_000 * 1024 * 1024),
    )
    raw = {
        "concurrency_levels": [1],
        "snapshot_disk_reserve_mib": 10,
        "resources": {"runtime_memory_mib": 2048, "tool_memory_mib": 4096},
        "paper_experiment": {
            "dimension": "temporal", "admission_policies": ["p90"],
            "reclamation_policies": ["checkpoint"],
            "decision_policies": ["fixed_delay"],
        },
    }
    pair = validate_disk_readiness(raw, tmp_path)
    assert pair["snapshot_generation_bound"] == 2
    assert pair["required_mib"] == 2 * (2048 + 4096) + 10

    raw["reclamation"] = {"checkpoint_scope": "tool"}
    tool = validate_disk_readiness(raw, tmp_path)
    assert tool["required_mib"] == 2 * 4096 + 10


def test_paper_vm_pool_requires_parent_and_distinct_runtime_tool_children(tmp_path):
    from clawbox.replay.suite import (
        validate_tool_pool_memory,
        validate_vm_pool_memory,
    )

    parent = tmp_path / "vm"
    runtime = parent / "runtime"
    tool = parent / "tool"
    for path in (parent, runtime, tool):
        path.mkdir(parents=True, exist_ok=True)
        (path / "cgroup.procs").write_text("", encoding="ascii")
    (parent / "memory.max").write_text(str(1000 * 1024 * 1024), encoding="ascii")
    (tool / "memory.max").write_text(str(700 * 1024 * 1024), encoding="ascii")
    raw = {
        "paper_experiment": {"dimension": "spatial"},
        "tool_pool_memory": {
            "cgroup": str(tool), "hard_limit_mib": 700,
            "high_watermark_mib": 600, "low_watermark_mib": 500,
            "headroom_mib": 50,
        },
        "vm_pool_memory": {
            "cgroup": str(parent), "runtime_cgroup": str(runtime),
            "hard_limit_mib": 1000, "high_watermark_mib": 900,
            "low_watermark_mib": 800, "headroom_mib": 50,
            "initial_runtime_rss_mib": 10, "initial_tool_rss_mib": 20,
            "restore_transient_headroom_mib": 30,
        },
    }
    tool_config = validate_tool_pool_memory(raw, tmp_path)
    vm_config = validate_vm_pool_memory(raw, tmp_path, tool_config)
    assert vm_config["runtime_cgroup"] == str(runtime)
    assert vm_config["restore_transient_headroom_mib"] == 30

    raw["vm_pool_memory"]["runtime_cgroup"] = str(tool)
    with pytest.raises(ValueError, match="distinct"):
        validate_vm_pool_memory(raw, tmp_path, tool_config)


def test_fair_semaphore_times_out_without_stranding_next_ticket() -> None:
    from clawbox.replay.lifecycle import FairSemaphore

    slots = FairSemaphore(1)
    assert slots.acquire() is True
    outcomes: list[bool] = []

    first = threading.Thread(target=lambda: outcomes.append(slots.acquire(timeout=0.02)))
    first.start()
    first.join(timeout=1)
    assert outcomes == [False]
    slots.release()
    assert slots.acquire(timeout=0.1) is True
    slots.release()


def test_fair_resource_pool_leases_unique_cpu_pairs_fifo() -> None:
    from clawbox.replay.lifecycle import FairResourcePool

    pool = FairResourcePool([(0, 1), (2, 3)])
    first = pool.acquire()
    second = pool.acquire()
    assert {first, second} == {(0, 1), (2, 3)}
    outcomes: list[tuple[int, int] | None] = []

    waiter = threading.Thread(
        target=lambda: outcomes.append(pool.acquire(timeout=1.0))
    )
    waiter.start()
    time.sleep(0.02)
    assert outcomes == []
    pool.release(first)
    waiter.join(timeout=1)
    assert outcomes == [first]
    pool.release(second)
    pool.release(outcomes[0])


def test_fair_resource_pool_timeout_does_not_strand_next_ticket() -> None:
    from clawbox.replay.lifecycle import FairResourcePool

    pool = FairResourcePool([(0, 1)])
    lease = pool.acquire()
    assert pool.acquire(timeout=0.01) is None
    pool.release(lease)
    assert pool.acquire(timeout=0.1) == lease


def test_fair_weighted_pool_accounts_heterogeneous_fifo_reservations() -> None:
    from clawbox.replay.lifecycle import FairWeightedResourcePool

    pool = FairWeightedResourcePool(10)
    large = pool.acquire(7)
    assert large is not None
    outcomes: list[int | None] = []
    waiter = threading.Thread(target=lambda: outcomes.append(pool.acquire(4, timeout=1)))
    waiter.start()
    time.sleep(0.02)
    assert outcomes == []
    small = pool.acquire(3, timeout=0.01)
    assert small is None  # FIFO: it cannot bypass the earlier 4-unit waiter.
    pool.release(large)
    waiter.join(timeout=1)
    assert outcomes[0] is not None
    pool.release(outcomes[0])


def test_feedback_memory_admission_remeasures_instead_of_reclaiming_prediction() -> None:
    from clawbox.replay.lifecycle import FeedbackMemoryAdmission

    resident = [100]
    admission = FeedbackMemoryAdmission(
        1000, 100, lambda: resident[0], poll_s=0.001,
    )
    first = admission.acquire(300, timeout=0.1)
    assert first is not None
    first_lease, first_status = first
    assert first_status["admission_charge_bytes"] == 500

    # Realized growth is already present in live RSS, so only the unrealized
    # remainder remains reserved. Growth beyond the prediction is counted once.
    resident[0] = 450
    observed = admission.observe()
    assert observed["outstanding_unrealized_growth_bytes"] == 0
    assert observed["admission_charge_bytes"] == 550
    assert admission.acquire(300, timeout=0.01) is not None
    assert admission.acquire(200, timeout=0.01) is None
    assert admission.metrics()["prediction_exceeded_leases"] >= 1

    # Completion remeasures persistent RSS before dropping only this lease's
    # remaining unrealized commitment.
    resident[0] = 500
    released = admission.release(first_lease)
    assert released["resident_bytes"] == 500
    assert released["admission_charge_bytes"] == 850


def test_feedback_memory_admission_tracks_realization_per_tool_vm() -> None:
    from clawbox.replay.lifecycle import FeedbackMemoryAdmission

    tool_a = [100]
    tool_b = [100]
    admission = FeedbackMemoryAdmission(
        1000, 100, lambda: tool_a[0] + tool_b[0], poll_s=0.001,
    )
    first = admission.acquire(200, timeout=0.1, measure_resident_bytes=lambda: tool_a[0])
    second = admission.acquire(200, timeout=0.1, measure_resident_bytes=lambda: tool_b[0])
    assert first is not None and second is not None
    tool_a[0] = 250
    status = admission.observe()
    assert status["resident_bytes"] == 350
    assert status["outstanding_unrealized_growth_bytes"] == 250
    assert status["admission_charge_bytes"] == 700


def test_feedback_memory_admission_observes_checkpoint_reclamation() -> None:
    from clawbox.replay.lifecycle import FeedbackMemoryAdmission

    resident = [600]
    admission = FeedbackMemoryAdmission(1000, 100, lambda: resident[0])
    assert admission.observe()["admission_charge_bytes"] == 700
    resident[0] = 0  # Firecracker stopped after a verified checkpoint.
    assert admission.observe()["admission_charge_bytes"] == 100


def test_resident_slot_lifecycle_releases_budget_after_checkpoint() -> None:
    from clawbox.replay.lifecycle import FairSemaphore, ResidentSlotLifecycle, SimulatedLifecycle

    slots = FairSemaphore(1)
    first = ResidentSlotLifecycle(SimulatedLifecycle(), slots)
    second = ResidentSlotLifecycle(SimulatedLifecycle(), slots)
    first.start()
    completed = threading.Event()

    def start_second() -> None:
        second.start()
        completed.set()

    waiter = threading.Thread(target=start_second)
    waiter.start()
    time.sleep(0.02)
    assert not completed.is_set()
    first.checkpoint_and_evict()
    assert completed.wait(1)
    second.close()
    first.close()


def test_suite_requires_prediction_for_the_exact_held_out_trace(tmp_path: Path) -> None:
    from clawbox.replay.suite import _prediction_provenance

    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"action"}\n', encoding="utf-8")
    trace_digest = __import__("hashlib").sha256(trace.read_bytes()).hexdigest()
    prediction = tmp_path / "prediction.json"
    prediction.write_text(json.dumps({
        "source_digest": "a" * 64,
        "pair_digest": "b" * 64,
        "artifact_count": 20,
        "training": {"runs": [{"run_id": "session-0001"}]},
        "evaluation": {
            "protocol": "leave-one-recording-out",
            "held_out_workload": "rec-a",
            "held_out_run_id": "session-0000",
            "held_out_trace_sha256": trace_digest,
        },
    }), encoding="utf-8")

    provenance = _prediction_provenance(
        prediction, workload_name="rec-a", trace_path=trace, require_held_out=True,
    )
    assert provenance["evaluation"]["held_out_run_id"] == "session-0000"
    with pytest.raises(ValueError, match="another workload"):
        _prediction_provenance(
            prediction, workload_name="rec-b", trace_path=trace, require_held_out=True,
        )


def test_engine_hashes_final_state_validation() -> None:
    class Executor:
        def execute(self, command: str, timeout_s: float):
            assert command == "check-state"
            return SimpleNamespace(exit_code=0, stdout="stable", stderr="", duration_s=0.1)

    summary = ReplayEngine(
        lifecycle=SimulatedLifecycle(), executor=Executor(),
        predictor=LinearLatencyPredictor.from_actions([]),
        policy=SnapshotPolicy(), mode="resident", sleep_scale=0,
        validation_command="check-state",
    ).run([])
    assert summary.validation_sha256 == __import__("hashlib").sha256(b"stable").hexdigest()


def test_guest_ssh_controller_requires_requests_and_guest_completion(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "run-guest-ssh-smoke.py"
    spec = importlib.util.spec_from_file_location("guest_ssh_smoke", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = tmp_path / "requests.json"
    log = tmp_path / "runtime.log"
    store.write_text(json.dumps([{"request_id": "a", "ready": True}]))
    log.write_text('boot\n{"ok":true,"actions":2}\n')
    assert module._wait_complete(store, log, 1, 0.1)[0]["request_id"] == "a"
    with pytest.raises(TimeoutError):
        module._wait_complete(store, log, 2, 0.01)


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


def test_firecracker_uses_long_timeout_only_for_snapshot_io(
    tmp_path: Path, monkeypatch,
) -> None:
    observed: list[float] = []

    class Client:
        def __init__(self, _path: Path, timeout_s: float) -> None:
            observed.append(timeout_s)

        def request(self, *_args, **_kwargs):
            return None

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(lifecycle_module, "_UnixHttpClient", Client)
    config = FirecrackerConfig(
        binary=tmp_path / "firecracker", api_socket=tmp_path / "api.sock",
        kernel_image=tmp_path / "kernel", rootfs=tmp_path / "rootfs",
        snapshot_state=tmp_path / "state", snapshot_memory=tmp_path / "memory",
        api_timeout_s=7.0, snapshot_api_timeout_s=101.0,
    )
    lifecycle = FirecrackerLifecycle(config)
    lifecycle._spawn = lambda: setattr(lifecycle, "_process", Process())
    lifecycle.start()
    assert observed == [7.0]
    lifecycle._stop_process = lambda: setattr(lifecycle, "_process", None)
    lifecycle.checkpoint_and_evict()
    assert observed == [7.0, 101.0]
    lifecycle._spawn = lambda: setattr(lifecycle, "_process", Process())
    lifecycle.restore()
    assert observed == [7.0, 101.0, 101.0]


def test_firecracker_balloon_is_preboot_configured_and_live_adjustable(
    tmp_path: Path, monkeypatch,
) -> None:
    requests: list[tuple[str, str, object]] = []

    class Client:
        def __init__(self, _path: Path, _timeout_s: float) -> None:
            pass

        def request(self, method: str, route: str, payload=None):
            requests.append((method, route, payload))
            if route == "/balloon/statistics":
                return {"target_mib": 1536, "actual_mib": 1500}
            return None

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(lifecycle_module, "_UnixHttpClient", Client)
    config = FirecrackerConfig(
        binary=tmp_path / "firecracker", api_socket=tmp_path / "api.sock",
        kernel_image=tmp_path / "kernel", rootfs=tmp_path / "rootfs",
        snapshot_state=tmp_path / "state", snapshot_memory=tmp_path / "memory",
        memory_mib=2048, balloon_enabled=True,
    )
    lifecycle = FirecrackerLifecycle(config)
    lifecycle._spawn = lambda: setattr(lifecycle, "_process", Process())
    lifecycle.start()
    assert ("PUT", "/balloon", {
        "amount_mib": 0, "deflate_on_oom": True,
        "stats_polling_interval_s": 1,
    }) in requests
    assert requests.index(("PUT", "/balloon", {
        "amount_mib": 0, "deflate_on_oom": True,
        "stats_polling_interval_s": 1,
    })) < next(i for i, item in enumerate(requests) if item[1] == "/actions")
    assert lifecycle.set_balloon_target_mib(1536) == {
        "target_mib": 1536, "actual_mib": 1500,
    }
    assert ("PATCH", "/balloon", {"amount_mib": 1536}) in requests
    with pytest.raises(ValueError, match="below VM capacity"):
        lifecycle.set_balloon_target_mib(2048)


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
