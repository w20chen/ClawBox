from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.client import RemoteDisconnected
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from clawbox.experiments.model_gateway import ManagedModelGateway


def write_trace(path: Path, *, prefix: str = "") -> dict:
    payloads = []
    for index in range(2):
        payload = {"messages": [{"role": "user", "content": f"{prefix}step-{index}"}]}
        payloads.append(payload)
    path.write_text(
        "".join(json.dumps({
            "type": "action", "action_type": "llm_call",
            "action_id": f"model-{index}", "ts_start": index,
            "ts_end": index + 0.01,
            "data": {
                "model": "test-model", "raw_request": payload,
                "raw_response": {"content": f"reply-{prefix}{index}"},
                "llm_latency_ms": 1,
            },
        }) + "\n" for index, payload in enumerate(payloads)),
        encoding="utf-8",
    )
    return {"messages": [{"role": "user", "content": f"{prefix}step-0"}]}


def post(url: str, token: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url + "/chat/completions", data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def test_managed_gateway_keeps_replay_cursors_and_delivery_state_session_local(tmp_path: Path) -> None:
    trace_a = tmp_path / "a.jsonl"
    trace_b = tmp_path / "b.jsonl"
    payload_a = write_trace(trace_a, prefix="a-")
    payload_b = write_trace(trace_b, prefix="b-")
    gateway = ManagedModelGateway(
        advertise_host="127.0.0.1", advertised_port=0, bind_host="127.0.0.1", bind_port=0,
    )
    with gateway:
        first = gateway.register(
            session_id="session-a", store_path=tmp_path / "a-store.json",
            mode="replay", trace=trace_a, time_scale=0,
        )
        second = gateway.register(
            session_id="session-b", store_path=tmp_path / "b-store.json",
            mode="replay", trace=trace_b, time_scale=0,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(
                lambda item: post(gateway.url, item[0].token, item[1]),
                [(first, payload_a), (second, payload_b)],
            ))
        assert [item["choices"][0]["message"]["content"] for item in responses] == [
            "reply-a-0", "reply-b-0"
        ]
        assert first.gateway.logical_model_steps() == 1
        assert second.gateway.logical_model_steps() == 1
        assert [event["event"] for event in first.records()[0]["lifecycle_events"]] == [
            "ModelRequestStarted", "ModelResponseGenerated",
            "ModelResponseReleased", "ModelResponseDelivered",
        ]
        assert first.records()[0]["replay_index"] == 0
        assert second.records()[0]["replay_index"] == 0
        assert first.records()[0]["request_namespace"] == "session-a"
        assert second.records()[0]["request_namespace"] == "session-b"
        assert gateway.session_count == 2
        assert first.drain(1)
        assert second.drain(1)
        assert first.replay_completeness()["complete"] is False  # second entry is intentionally unconsumed
        gateway.unregister(first.token, timeout=1)
        gateway.unregister(second.token, timeout=1)
        assert gateway.session_count == 0


def test_managed_gateway_api_http_path_keeps_upstream_credentials_server_side(
    tmp_path: Path,
) -> None:
    upstream_requests: list[tuple[dict, str]] = []

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            upstream_requests.append((payload, self.headers.get("Authorization", "")))
            body = json.dumps({
                "id": "chatcmpl-managed-api",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "managed-api-ok",
                }, "finish_reason": "stop"}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    gateway = ManagedModelGateway(
        advertise_host="127.0.0.1", advertised_port=0,
        bind_host="127.0.0.1", bind_port=0,
    )
    try:
        with gateway:
            session = gateway.register(
                session_id="api-session", store_path=tmp_path / "api-store.json",
                mode="api", trace=None, time_scale=0,
                upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                upstream_api_key="upstream-secret",
                upstream_model="server-model",
            )
            response = post(gateway.url, session.token, {
                "model": "guest-model",
                "messages": [{"role": "user", "content": "hello"}],
            })
            assert response["choices"][0]["message"]["content"] == "managed-api-ok"
            assert session.token != "upstream-secret"
            assert session.records()[0]["delivered"] is True
            assert session.replay_completeness()["complete"] is True
            replay_trace = tmp_path / "model-traces" / "api-export.jsonl"
            session.write_replay_trace(replay_trace)
            exported = json.loads(replay_trace.read_text(encoding="utf-8").splitlines()[0])
            assert exported["action_type"] == "llm_call"
            assert exported["data"]["model"] == "server-model"
            assert exported["data"]["raw_response"]["content"] == "managed-api-ok"
    finally:
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)

    assert upstream_requests == [(
        {"model": "server-model", "messages": [
            {"role": "user", "content": "hello"},
        ]},
        "Bearer upstream-secret",
    )]


def test_managed_gateway_retries_do_not_create_logical_steps(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    payload = write_trace(trace)
    started = []
    gateway = ManagedModelGateway(
        advertise_host="127.0.0.1", advertised_port=0, bind_host="127.0.0.1", bind_port=0,
    )
    with gateway:
        session = gateway.register(
            session_id="retry-session", store_path=tmp_path / "store.json",
            mode="replay", trace=trace, time_scale=0,
            on_request_started=lambda event: started.append(event["request_id"]),
        )
        first_status, first_type, first_body, request_id = session.gateway.complete_http(payload)
        second_status, second_type, second_body, retry_id = session.gateway.complete_http(payload)
        session.mark_delivery(request_id, delivered=True)
        assert (first_status, first_type, first_body) == (second_status, second_type, second_body)
        assert request_id == retry_id
        assert session.gateway.logical_model_steps() == 1
        assert len(started) == 1
        records = session.records()
        assert records[0]["http_attempts"] == 2
        assert records[0]["reconnect_attempts"] == 1


def test_runtime_checkpoint_invalidates_old_http_attempt_but_not_logical_step(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    payload = write_trace(trace)
    started = threading.Event()
    gateway = ManagedModelGateway(
        advertise_host="127.0.0.1", advertised_port=0,
        bind_host="127.0.0.1", bind_port=0,
    )
    with gateway:
        session = gateway.register(
            session_id="checkpoint-session", store_path=tmp_path / "store.json",
            mode="replay", trace=trace, time_scale=50,
            on_request_started=lambda _event: started.set(),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            old_attempt = pool.submit(post, gateway.url, session.token, payload)
            assert started.wait(1)
            session.invalidate_pending_delivery()
            with pytest.raises(RemoteDisconnected):
                old_attempt.result(timeout=3)

        retry_payload = {
            "messages": [*payload["messages"], payload["messages"][-1]],
        }
        response = post(gateway.url, session.token, retry_payload)
        assert response["choices"][0]["message"]["content"] == "reply-0"
        deadline = time.monotonic() + 1
        while not session.records()[0]["delivered"] and time.monotonic() < deadline:
            time.sleep(0.01)
        records = session.records()
        assert len(records) == 1
        assert records[0]["http_attempts"] == 2
        assert records[0]["reconnect_attempts"] == 1
        assert records[0]["delivery_failures"] == 1
        assert records[0]["delivered"] is True


def test_checkpoint_retry_artifact_is_removed_from_later_replay_steps(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    initial = {"messages": [{"role": "user", "content": "task"}]}
    continued = {"messages": [
        *initial["messages"],
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "exec", "arguments": '{"command":"true"}'},
        }]},
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
    ]}
    trace.write_text("".join(json.dumps({
        "type": "action", "action_type": "llm_call",
        "action_id": f"model-{index}", "ts_start": index, "ts_end": index,
        "data": {"model": "test-model", "raw_request": payload,
                 "raw_response": {"content": f"reply-{index}"},
                 "llm_latency_ms": 0},
    }) + "\n" for index, payload in enumerate((initial, continued))), encoding="utf-8")

    gateway = ManagedModelGateway(
        advertise_host="127.0.0.1", advertised_port=0,
        bind_host="127.0.0.1", bind_port=0,
    )
    with gateway:
        session = gateway.register(
            session_id="checkpoint-history", store_path=tmp_path / "store.json",
            mode="replay", trace=trace, time_scale=0,
        )
        _, _, _, request_id = session.gateway.complete_http(initial)
        session.mark_delivery(request_id, delivered=False)
        duplicate_initial = {"messages": [initial["messages"][0], initial["messages"][0]]}
        _, _, _, retry_id = session.gateway.complete_http(duplicate_initial)
        assert retry_id == request_id
        session.mark_delivery(retry_id, delivered=True)

        continued_with_retry_artifact = {
            "messages": [initial["messages"][0], initial["messages"][0],
                         *continued["messages"][1:]],
        }
        _, _, _, second_id = session.gateway.complete_http(continued_with_retry_artifact)
        session.mark_delivery(second_id, delivered=True)
        assert session.gateway.logical_model_steps() == 2
        assert session.replay_completeness()["complete"] is True
        assert session.records()[1]["replay_input_match_mode"] == (
            "volatile_fields_v1+checkpoint_reconnect_v1"
        )


def test_duplicate_user_turn_without_checkpoint_retry_still_fails_closed(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    payload = write_trace(trace)
    gateway = ManagedModelGateway(
        advertise_host="127.0.0.1", advertised_port=0,
        bind_host="127.0.0.1", bind_port=0,
    )
    with gateway:
        session = gateway.register(
            session_id="ordinary-duplicate", store_path=tmp_path / "store.json",
            mode="replay", trace=trace, time_scale=0,
        )
        duplicate = {"messages": [payload["messages"][0], payload["messages"][0]]}
        with pytest.raises(ValueError, match="replay request diverged"):
            session.gateway.complete_http(duplicate)


def test_request_started_callback_does_not_create_gateway_hol(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    payload = write_trace(trace)
    entered = threading.Event()
    release = threading.Event()

    def slow_callback(_event: dict) -> None:
        entered.set()
        release.wait(2)

    gateway = ManagedModelGateway(
        advertise_host="127.0.0.1", advertised_port=0, bind_host="127.0.0.1", bind_port=0,
    )
    with gateway:
        slow = gateway.register(
            session_id="slow", store_path=tmp_path / "slow-store.json",
            mode="replay", trace=trace, time_scale=0, on_request_started=slow_callback,
        )
        fast = gateway.register(
            session_id="fast", store_path=tmp_path / "fast-store.json",
            mode="replay", trace=trace, time_scale=0,
        )
        slow_payload = payload
        fast_payload = {"messages": [{"role": "user", "content": "step-0"}]}
        # Fast has a different trace-independent payload only to prove that it
        # does not wait on slow session callback execution.
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow_future = pool.submit(post, gateway.url, slow.token, slow_payload)
            assert entered.wait(1)
            started = time.monotonic()
            fast_future = pool.submit(post, gateway.url, fast.token, fast_payload)
            fast_future.result(timeout=2)
            assert time.monotonic() - started < 1
            release.set()
            slow_future.result(timeout=2)
