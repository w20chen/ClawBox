"""Real local HTTP/SSE checks; no native simulator or VM dependencies."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ast
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from queue import Empty, Queue
import select
import socket
from threading import Event, Lock, Thread
import time
from types import SimpleNamespace
from typing import Any

import pytest
from trace_fixtures import llm_spans, write_spans

from clawbox.experiments.model_gateway import SessionGatewayState, ManagedModelGateway
from clawbox.experiments.policy import PolicyCoordinator, PolicyEventExecutor
from clawbox.experiments.serving_sim_client import ServingSimClient
from clawbox.experiments.spec import PolicySpec
from clawbox.experiments.spec_types import EvictionPolicy


@contextmanager
def simulator(mode="success", *, clock_offset_s=0., info_override=None):
    state = SimpleNamespace(mode=mode, posts=[], signals=Queue(), started=Event(),
                            disconnected=Event(), finish=Event(), stop=Event())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def send_headers(self, content_type, length=None):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()
            self.wfile.flush()

        def emit(self, data):
            self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
            self.wfile.flush()

        def do_GET(self):
            if self.path == "/v1/clawbox/simulator/info":
                body = json.dumps({"ready": True, "clock": {"k": 1},
                    "adapter_protocol": 2,
                    "traces": {"trace": {"model_calls": 1,
                        "missing_reasoning_calls": int(state.mode == "no_reasoning")}},
                    "accepted_requests": 0, "catalog_digest": "catalog",
                    "service_id": "epoch", "server_wall_time": time.time() + clock_offset_s,
                    "signals": {"head": "epoch:0"}, **(info_override or {})}).encode()
                self.send_headers("application/json", len(body))
                self.wfile.write(body)
                return
            assert self.path.startswith("/request-signals/clawbox/events?")
            self.send_headers("text/event-stream")
            try:
                while not state.stop.is_set():
                    try:
                        kind, identity, record = state.signals.get(timeout=.05)
                    except Empty:
                        self.wfile.write(b": keepalive\n\n")
                    else:
                        self.wfile.write((f"event: {kind}\nid: {identity}\ndata: "
                                          + json.dumps(record) + "\n\n").encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError):
                pass

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.posts.append(body)
            state.started.set()
            self.send_headers("text/event-stream")
            if state.mode == "error":
                self.emit({"error": "synthetic failure"})
                return
            binding = dict(id=body["request_id"], request_id=body["request_id"],
                           session_id=body["session_id"], replay=body["replay"], t=1.)
            if state.mode == "wrong_binding":
                binding["replay"] = {"trace_id": "different", "call_id": 0}
            self.wfile.write(('event: clawbox\ndata: ' + json.dumps(dict(protocol=2,
                session_id=body['session_id'], request_id=body['request_id'],
                replay=body['replay'], process_sessions={})) + '\n\n').encode())
            self.emit({**binding, "choices": [{"index": 0,
                "delta": {"content": "actual GPU output"}, "finish_reason": None}]})
            if state.mode == "truncated":
                return
            if state.mode == "incomplete_event":
                self.wfile.write(b'data: {"partial":')
                self.wfile.flush()
                return
            signal_fields = (dict(event="first_token") if state.mode == "no_reasoning"
                             else dict(event="think_end", source="trace"))
            state.signals.put(("signal", "epoch:1", dict(
                **signal_fields, role="A", engine_id="card-0",
                session_id=body["session_id"], request_id=body["request_id"],
                wall_t=time.time() + clock_offset_s, t=1.)))
            try:
                while not state.finish.wait(.01) and not state.stop.is_set():
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if readable and not self.connection.recv(1, socket.MSG_PEEK):
                        state.disconnected.set()
                        return
                if state.stop.is_set():
                    return
                self.emit({**binding, "choices": [{"index": 0, "delta": {},
                                                    "finish_reason": "stop"}]})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError):
                state.disconnected.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        state.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture
def client_factory(monkeypatch, tmp_path):
    def make(server):
        monkeypatch.setattr("clawbox.experiments.serving_sim_inputs.load_serving_sim_manifest",
                            lambda *_args, **_kwargs: {"catalog": {"catalog_digest": "catalog"},
                                "cases": [dict(case_id='case', trace_id='trace', model_calls=1,
                                               missing_reasoning_calls=int(server.mode == "no_reasoning"))]})
        return ServingSimClient({"base_url": server.url, "model": "test-model",
            "serving_sim_catalog_manifest": str(tmp_path / "manifest.json")}, "", tmp_path / "client")

    return make


def complete(client, callback=None, timeout=3):
    return client.complete(session_id="session", request_id="request", trace_id="trace",
        call_id=0, payload={"messages": [{"role": "user", "content": "real prompt"}],
                            "tools": [{"type": "function", "function": {"name": "exec"}}]},
        on_signal=callback, timeout_s=timeout)


@pytest.mark.parametrize("count", [None, True, 1])
def test_service_must_explicitly_confirm_no_previous_requests(client_factory, count):
    with simulator(info_override={"accepted_requests": count}) as server:
        with pytest.raises(ValueError, match="restart"):
            with client_factory(server):
                pass
        assert not server.posts


def test_expected_topology_is_checked_before_model_submission(client_factory):
    with simulator(info_override={"topology_digest": "actual"}) as server:
        client = client_factory(server)
        client.configuration["serving_sim_topology_digest"] = "different"
        with pytest.raises(ValueError, match="topology differs"):
            with client:
                pass
        assert not server.posts


def test_host_observes_think_end_while_response_stream_is_still_running(client_factory):
    observed, records = Event(), []

    def callback(record):
        records.append(record)
        observed.set()

    with simulator() as server, client_factory(server) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(complete, client, callback)
            try:
                assert observed.wait(2)
                assert not pending.done()
                assert records[0]["event"] == "think_end"
                assert records[0]["source"] == "trace"
                assert records[0]["signal_sequence"] == 1
                assert server.posts[0]["messages"][0]["content"] == "real prompt"
                assert server.posts[0]["tools"][0]["function"]["name"] == "exec"
                assert server.posts[0]["replay"] == {"trace_id": "trace", "call_id": 0}
            finally:
                server.finish.set()
            status, content_type, body = pending.result(timeout=2)
            assert status == 200 and content_type == "text/event-stream"
            assert b"actual GPU output" in body
            assert body.endswith(b"data: [DONE]\n\n")


def test_missing_reasoning_completes_without_think_end_or_cpu_trace(client_factory):
    observed, records = Event(), []

    def callback(record):
        records.append(record)
        observed.set()

    with simulator("no_reasoning") as server, client_factory(server) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(complete, client, callback)
            try:
                assert observed.wait(2)
                assert [r["event"] for r in records] == ["first_token"]
                assert not pending.done()
            finally:
                server.finish.set()
            status, _, body = pending.result(timeout=2)
            assert status == 200
            assert b"actual GPU output" in body
            assert len(server.posts) == 1


@pytest.mark.parametrize("clock_offset_s", [-120., 120.])
def test_gpu_signal_age_uses_measured_host_clock_offset(client_factory, clock_offset_s):
    observed, records = Event(), []

    def callback(record):
        records.append(record)
        observed.set()

    with simulator(clock_offset_s=clock_offset_s) as server, client_factory(server) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(complete, client, callback)
            try:
                assert observed.wait(2)
                assert abs(client.clock_offset_s + clock_offset_s) < 1
                assert 0 <= records[0]["signal_age_seconds"] < 1
                assert client.info["client_clock_alignment"]["uncertainty_s"] >= 0
            finally:
                server.finish.set()
            pending.result(timeout=2)


@pytest.mark.parametrize("mode, error", [
    ("error", "error event"), ("truncated", "terminal response and DONE"),
    ("incomplete_event", "incomplete event"), ("wrong_binding", "replay binding mismatch"),
])
def test_http_200_does_not_mask_failed_or_truncated_gpu_output(client_factory, mode, error):
    with simulator(mode) as server, client_factory(server) as client:
        with pytest.raises(RuntimeError, match=error):
            complete(client)
        assert len(server.posts) == 1


def test_timeout_closes_the_real_upstream_http_stream(client_factory):
    with simulator() as server, client_factory(server) as client:
        with pytest.raises(TimeoutError, match="timed out"):
            complete(client, timeout=.15)
        assert server.started.is_set()
        assert server.disconnected.wait(2)
        assert len(server.posts) == 1


def test_explicit_cancel_closes_the_real_upstream_http_stream(client_factory):
    with simulator() as server, client_factory(server) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(complete, client)
            assert server.started.wait(2)
            client.cancel("request")
            with pytest.raises(RuntimeError, match="cancelled"):
                pending.result(timeout=2)
            assert server.disconnected.wait(2)


def test_signal_history_gap_fails_the_arm_and_cancels_pending_gpu_work(client_factory):
    with simulator() as server, client_factory(server) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(complete, client)
            assert server.started.wait(2)
            server.signals.put(("gap", "epoch:2", {"error": "expired"}))
            with pytest.raises(RuntimeError, match="observation failed.*gap"):
                pending.result(timeout=2)
            assert server.disconnected.wait(2)
            with pytest.raises(RuntimeError, match="observation failed"):
                client.check()


def session_state(tmp_path, client, on_signal=None):
    return SessionGatewayState(session_id="session", token="token", store_path=tmp_path / "gateway.json",
        mode="serving_sim", trace=None, time_scale=1, upstream_base_url=None,
        upstream_api_key=None, upstream_model=None, request_timeout_s=3,
        serving_sim=client, serving_sim_trace_id="trace", serving_sim_model_calls=1,
        on_model_signal=on_signal)


def test_policy_start_failure_wakes_retries_and_never_submits_gpu(client_factory, tmp_path):
    entered, release = Event(), Event()
    def fail(event):
        entered.set()
        assert release.wait(2)
        raise ValueError('waiting registration failed')
    with simulator() as server, client_factory(server) as client:
        state = session_state(tmp_path, client)
        state.gateway.on_request_started = fail
        payload = dict(messages=[], stream=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(state.complete_http, payload, logical_request_id='same')
            assert entered.wait(2)
            retry = pool.submit(state.complete_http, payload, logical_request_id='same')
            release.set()
            for pending in (first, retry):
                with pytest.raises(RuntimeError, match='policy failed'):
                    pending.result(2)
        assert not server.posts
        assert not state.replay_completeness()['complete']
        records = state.records()
        assert len(records) == 1 and records[0]['ready'] and records[0]['error']


def test_compact_stream_assembles_fragmented_tools_and_reasoning():
    from clawbox.experiments.serving_sim_client import _Message
    message = _Message()
    message.append(dict(role='assistant', reasoning_content='think', tool_calls=[dict(index=0,
        id='call', type='function', function=dict(name='process', arguments=''))]))
    arguments = '{"sessionId":"recorded-handle"}'
    for character in arguments:
        message.append(dict(tool_calls=[dict(index=0, function=dict(arguments=character))]))
    message.append(dict(content='done'))
    assert message.value() == dict(role='assistant', content='done', reasoning_content='think',
        tool_calls=[dict(index=0, id='call', type='function', function=dict(name='process', arguments=arguments))])


def test_managed_http_gateway_serves_gpu_without_cpu_trace(client_factory, tmp_path):
    import httpx
    with simulator() as server, client_factory(server) as client, ManagedModelGateway(
        advertise_host='127.0.0.1', advertised_port=0, bind_host='127.0.0.1', bind_port=0,
    ) as gateway:
        state = gateway.register(session_id='session', store_path=tmp_path / 'managed.json',
            mode='serving_sim', trace=None, serving_sim=client, serving_sim_trace_id='trace',
            serving_sim_model_calls=1, serving_sim_allow_background=True)
        server.finish.set()
        with httpx.Client(trust_env=False) as http:
            response = http.post(gateway.url + '/chat/completions',
                headers={'Authorization': 'Bearer ' + state.token, 'X-ClawBox-Request-ID': 'request-0'},
                json=dict(messages=[], stream=True))
        assert response.status_code == 200, response.text
        assert 'actual GPU output' in response.text
        assert state.drain(2)
        assert state.replay_completeness()['complete']
        assert state.gateway.actions == []
        assert state.gateway.serving_sim_allow_background is True
        assert len(server.posts) == 1


def test_checkpoint_reconnect_and_post_delivery_retry_share_one_gpu_execution(client_factory, tmp_path):
    observed = Event()
    with simulator() as server, client_factory(server) as client:
        state = session_state(tmp_path, client, lambda _record: observed.set())
        payload = {"messages": [], "stream": True}
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(state.complete_http, payload, logical_request_id="logical-1")
            try:
                assert observed.wait(2)
                generation = state.delivery_generation()
                state.invalidate_pending_delivery()
                assert not state.may_deliver(generation)
                retry = pool.submit(state.complete_http, payload, logical_request_id="logical-1")
                assert not server.disconnected.is_set()
            finally:
                server.finish.set()
            original = first.result(timeout=2)
            assert retry.result(timeout=2) == original
        state.mark_delivery(original[3], delivered=True)
        assert state.complete_http(payload, logical_request_id="logical-1") == original
        assert len(server.posts) == 1
        assert state.gateway.logical_model_steps() == 1
        record = state.records()[0]
        assert record["production_attempts"] == 1
        assert record["http_attempts"] == 3
        assert b"actual GPU output" in original[2]
        assert b"CPU recorded output" not in original[2]
        with pytest.raises(ValueError, match="different payload"):
            state.complete_http({"messages": [{"role": "user", "content": "changed"}]},
                                logical_request_id="logical-1")
        state.gateway.close()


def test_simulator_gateway_requires_explicit_logical_identity(client_factory, tmp_path):
    with simulator() as server, client_factory(server) as client:
        state = session_state(tmp_path, client)
        with pytest.raises(ValueError, match="X-ClawBox-Request-ID"):
            state.complete_http({"messages": []})
        assert not server.posts
        state.gateway.close()


def test_gateway_failure_does_not_publish_successful_response_callback(client_factory, tmp_path):
    callbacks = []
    with simulator("error") as server, client_factory(server) as client:
        state = session_state(tmp_path, client)
        state.gateway.before_response_ready = lambda *_args: callbacks.append("success") or {}
        with pytest.raises(RuntimeError, match="error event"):
            state.complete_http({"messages": []}, logical_request_id="logical-1")
        assert not callbacks
        assert state.records()[0]["error"]
        state.gateway.close()


def test_cancel_between_gateway_production_and_client_registration_never_submits_gpu(client_factory, tmp_path, monkeypatch):
    entered, release, finished = Event(), Event(), Event()
    with simulator() as server, client_factory(server) as client:
        original = client._complete
        async def delayed(*args):
            entered.set()
            assert await asyncio.to_thread(release.wait, 2)
            try:
                return await original(*args)
            finally:
                finished.set()
        monkeypatch.setattr(client, '_complete', delayed)
        state = session_state(tmp_path, client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(state.complete_http, {"messages": []}, logical_request_id="logical-1")
            try:
                assert entered.wait(2)
                state.gateway.cancel_pending()
                with pytest.raises(RuntimeError, match="session closed"):
                    pending.result(timeout=1)
                release.set()
                assert finished.wait(1), "cancelled production must not start a GPU request"
                assert not server.posts
            finally:
                release.set()
                server.finish.set()
                finished.wait(2)
                state.gateway.close()


def worker_prewarm_environment():
    """Run the actual nested worker callbacks with a stub native VM boundary."""
    policy = PolicySpec(name="gpu", admission="predicted", reclamation="snapshot_pause",
                        eviction="gpu_aware_pressure", restore="reactive")
    coordinator = PolicyCoordinator(policy, budget_mib=32, emergency_free_mib=0,
                                    operation_headroom_mib=0)
    runtime, tool = SimpleNamespace(resident=False), SimpleNamespace(resident=False)
    coordinator.register("session", tool)
    coordinator.register_runtime("session", runtime)
    coordinator.begin_model_wait("session", "request")
    events, restored, tool_ready = [], [], []
    executor = PolicyEventExecutor(workers=1)
    wait_lock = Lock()

    def restore(_arm, _sid, vm, _coordinator, _events, *, role):
        assert wait_lock.locked()
        assert vm is {"runtime": runtime, "tool": tool}[role]
        restored.append(role)
        vm.resident = True

    environment = dict(Any=Any, time=time, shlex=__import__("shlex"),
        EvictionPolicy=EvictionPolicy, arm=SimpleNamespace(policy=policy),
        coordinator=coordinator, session_id="session", runtime_snapshot_enabled=True,
        runtime_lifecycle=runtime, lifecycle=tool, session_binding=None,
        runtime_executor=object(), RELAY_CHECKPOINT_URL="http://127.0.0.1:8766/checkpoint",
        _execute_idempotent=lambda *_args: SimpleNamespace(exit_code=0),
        observe_openclaw_agent_pid=lambda _phase: 42, _record_time_span=lambda *_args, **_kwargs: None,
        timeline={}, wait_lock=wait_lock, generated_model_requests_lock=Lock(),
        wait_state={"request_id": "request", "runtime_agent_pid_before_pause": 42},
        generated_model_requests=set(), completed_model_requests=set(),
        self=SimpleNamespace(_restore_with_one_victim=restore),
        prepare_restored_tool=lambda **kwargs: tool_ready.append(kwargs), tool_ready=tool_ready,
        events=SimpleNamespace(write=events.append), policy_events=executor,
        pause_for_model_wait=lambda _event: None)
    source = Path(__file__).resolve().parents[1] / "clawbox/experiments/worker.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"on_model_signal", "prewarm_pair_from_gpu", "restore_runtime_for_model_wait_locked"}
    callbacks = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(callbacks) == len(names)
    module = ast.fix_missing_locations(ast.Module(body=callbacks, type_ignores=[]))
    exec(compile(module, str(source), "exec"), environment)
    return environment, restored, events, executor


@pytest.mark.parametrize("runtime_resident,tool_resident,expected", [
    (False, False, ["runtime", "tool"]),
    (True, False, ["tool"]),
    (False, True, ["runtime"]),
    (True, True, []),
])
def test_worker_think_end_prewarms_both_vms(runtime_resident, tool_resident, expected):
    env, restored, events, executor = worker_prewarm_environment()
    env["runtime_lifecycle"].resident = runtime_resident
    env["lifecycle"].resident = tool_resident
    try:
        signal = {"session_id": "session", "request_id": "request",
                  "event": "think_end", "role": "A", "source": "trace", "t": 2.}
        env["on_model_signal"](signal)
        env["on_model_signal"](signal)  # duplicate delivery must not repeat either restore
    finally:
        executor.close()
    executor.check()
    assert restored == expected
    assert env["runtime_lifecycle"].resident
    assert env["lifecycle"].resident
    assert env["tool_ready"] == ([] if tool_resident else [{"request_id": "request"}])
    if not tool_resident:
        assert env["wait_state"]["restore_request_id"] == "request"
        assert env["wait_state"]["restore_completed_at"] >= env["wait_state"]["restore_started_at"]
    if not runtime_resident:
        assert env["wait_state"]["runtime_restore_request_id"] == "request"
    assert any(event["event"] == "model_relay_reconnected" for event in events) is (not runtime_resident)
    assert sum(event["event"] == "gpu_pair_prewarm_ready" for event in events) == 1
    assert not env["coordinator"]._sessions["session"].tool_active
    assert not env["coordinator"]._sessions["session"].response_ready
    # The ordinary response-ready path observes the already resident Runtime.
    with env["wait_lock"]:
        env["restore_runtime_for_model_wait_locked"]({"request_id": "request"}, phase="response")
    assert restored == expected


def test_worker_tool_prewarm_readiness_failure_is_not_reported_ready():
    env, restored, events, executor = worker_prewarm_environment()

    def fail_readiness(**kwargs):
        raise RuntimeError("Tool bridge unavailable")

    env["prepare_restored_tool"] = fail_readiness
    try:
        env["on_model_signal"]({"session_id": "session", "request_id": "request",
                               "event": "think_end", "role": "D", "source": "trace", "t": 2.})
    finally:
        executor.close()
    with pytest.raises(RuntimeError, match="coalesced policy event failed") as failure:
        executor.check()
    assert str(failure.value.__cause__) == "Tool bridge unavailable"
    assert restored == ["runtime", "tool"]
    assert not any(event["event"] == "gpu_pair_prewarm_ready" for event in events)


def test_worker_queued_prewarm_yields_to_an_already_generated_response():
    env, restored, events, executor = worker_prewarm_environment()
    env["wait_lock"].acquire()
    try:
        env["on_model_signal"]({"session_id": "session", "request_id": "request",
                               "event": "think_end", "role": "D", "source": "trace", "t": 2.})
        # This is the completion callback's ordering: publish generation before
        # waiting for the lifecycle lock so queued prewarm does not duplicate it.
        with env["generated_model_requests_lock"]:
            env["generated_model_requests"].add("request")
        env["coordinator"].complete_model_wait("session", "request")
    finally:
        env["wait_lock"].release()
        executor.close()
    executor.check()
    assert not restored
    assert not any(event["event"] == "gpu_pair_prewarm_ready" for event in events)
