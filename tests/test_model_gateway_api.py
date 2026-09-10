from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from trace_fixtures import llm_spans, write_spans

from clawbox.replay.model_gateway import ModelGateway


@pytest.mark.parametrize("stream", [False, True])
def test_prefix_stop_is_separate_from_recorded_model_steps(tmp_path, stream):
    trace = tmp_path / "trace.jsonl"
    write_spans(trace, sum((llm_spans([], {"content": f"round {i}"}, index=i,
                                    duration_ms=0) for i in range(3)), []))
    original = trace.read_bytes()
    gateway = ModelGateway(tmp_path / "session.json", mode="replay", trace=trace,
                           max_model_steps=2)
    for i in range(2):
        payload = {"messages": [{"role": "user", "content": str(i)}]}
        _, _, body, request_id = gateway.complete_http(payload)
        assert json.loads(body)["choices"][0]["message"]["content"] == f"round {i}"
        # An undelivered response retry must not consume the next round.
        assert gateway.complete_http(payload)[3] == request_id
        gateway.mark_delivery(request_id, delivered=True)
    assert not gateway.replay_completeness()["complete"]
    request = {"messages": [{"role": "user", "content": "tools finished"}], "stream": stream}
    status, _, body, request_id = gateway.complete_http(request)
    assert status == 200 and b"configured replay round limit" in body
    gateway.mark_delivery(request_id, delivered=False)
    assert not gateway.replay_completeness()["complete"]
    assert gateway.complete_http(request)[2] == body
    gateway.mark_delivery(request_id, delivered=True)
    verdict = gateway.replay_completeness()
    assert verdict["complete"] and verdict["scope"] == "prefix"
    assert verdict["source_model_steps"] == 3
    assert verdict["expected_replay_model_steps"] == 2
    assert len(gateway.records()) == gateway.logical_model_steps() == 2
    assert json.loads((tmp_path / "session.prefix-stop.json").read_text())["synthetic"]
    assert trace.read_bytes() == original


@pytest.mark.parametrize("limit", [0, -1, True, "10", 1.5])
def test_prefix_limit_rejects_invalid_values(tmp_path, limit):
    trace = tmp_path / "trace.jsonl"
    write_spans(trace, llm_spans([], {"content": "done"}))
    with pytest.raises(ValueError, match="positive integer"):
        ModelGateway(tmp_path / "s.json", mode="replay", trace=trace, max_model_steps=limit)


def test_replay_trace_exhaustion_persists_the_unexpected_request(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    write_spans(trace, llm_spans([{"role": "user", "content": "first"}], {"content": "first response"}, duration_ms=100))
    gateway = ModelGateway(
        tmp_path / "not-created" / "session.json", mode="replay", trace=trace,
    )
    status, _content_type, _body, request_id = gateway.complete_http({
        "model": "recorded-model",
        "messages": [{"role": "user", "content": "first"}],
    })
    assert status == 200
    gateway.mark_delivery(request_id, delivered=True)

    unexpected = {
        "model": "recorded-model",
        "messages": [{"role": "user", "content": "second"}],
    }
    with pytest.raises(ValueError, match="more model calls"):
        gateway.complete(unexpected)

    rejection = tmp_path / "not-created" / "session.rejected-request-0001.json"
    record = json.loads(rejection.read_text(encoding="utf-8"))
    assert record["model_step"] == 1
    assert record["reason"] == "trace_exhausted"
    assert record["expected"] is None
    assert record["actual"]["messages"][0]["content"] == "second"
    with pytest.raises(ValueError, match="already diverged: trace_exhausted"):
        gateway.complete({
            "model": "recorded-model",
            "messages": [{"role": "user", "content": "first"}],
        })
    assert gateway.replay_completeness()["replay_failure"] == "trace_exhausted"


@pytest.mark.parametrize("actual_output", [
    "hash nt: 456\\nhash t: 456",
    "arbitrary task output with no known log format",
    "ERROR: the tool failed",
])
def test_replay_preserves_actual_tool_outputs_without_comparing_them(
    tmp_path: Path, actual_output: str,
) -> None:
    trace = tmp_path / "trace.jsonl"
    expected = [{"role": "tool", "tool_call_id": "call-1", "content": "recorded output"}]
    write_spans(trace, llm_spans(expected, {"content": "recorded response"}, duration_ms=0))
    gateway = ModelGateway(tmp_path / "session.json", mode="replay", trace=trace)
    actual = [{"role": "tool", "tool_call_id": "call-1", "content": actual_output}]
    status, _, body, request_id = gateway.complete_http({"messages": actual})
    gateway.mark_delivery(request_id, delivered=True)
    assert status == 200
    assert json.loads(body)["choices"][0]["message"]["content"] == "recorded response"
    assert gateway.records()[0]["request_payload"]["messages"] == actual
    verdict = gateway.replay_completeness()
    assert verdict["tool_output_comparison"] == "not_performed"
    assert verdict["complete"] is True
    # Gateway completion is not a task-validation verdict; even error text
    # stays untouched and the worker must still run the task's validation.


def test_api_gateway_forwards_model_and_keeps_upstream_credential_server_side(
    tmp_path: Path,
) -> None:
    requests: list[tuple[dict, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            requests.append((payload, self.headers.get("Authorization", "")))
            body = json.dumps({
                "id": "chatcmpl-test",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "api-path-ok",
                }, "finish_reason": "stop"}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = ModelGateway(
        tmp_path / "store.json", mode="api",
        upstream_base_url=f"http://127.0.0.1:{server.server_port}/v1",
        upstream_api_key="test-upstream-secret", upstream_model="server-model",
        timeout_s=5, request_namespace="api-session",
    )
    try:
        status, content_type, body, request_id = gateway.complete_http({
            "model": "guest-model",
            "messages": [{"role": "user", "content": "hello"}],
        })
        gateway.mark_delivery(request_id, delivered=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 200
    assert content_type == "application/json"
    assert json.loads(body)["choices"][0]["message"]["content"] == "api-path-ok"
    assert requests == [(
        {"model": "server-model", "messages": [{"role": "user", "content": "hello"}]},
        "Bearer test-upstream-secret",
    )]
    record = gateway.records()[0]
    assert record["production_attempts"] == 1
    assert record["delivered"] is True
    assert gateway.replay_completeness()["complete"] is True


def test_api_gateway_preserves_upstream_error_status(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = b'{"error":{"type":"authentication_error"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = ModelGateway(
        tmp_path / "store.json", mode="api",
        upstream_base_url=f"http://127.0.0.1:{server.server_port}/v1",
        upstream_api_key="invalid", upstream_model="server-model", timeout_s=5,
    )
    try:
        with pytest.raises(RuntimeError, match="upstream model returned HTTP 401"):
            gateway.complete_http({
                "model": "guest-model",
                "messages": [{"role": "user", "content": "hello"}],
            })
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    record = gateway.records()[0]
    assert record["status_code"] == 401
    assert record["error"] == "upstream model returned HTTP 401"
    assert gateway.replay_completeness()["complete"] is False
