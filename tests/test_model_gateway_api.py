from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from clawbox.replay.model_gateway import ModelGateway


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


def test_api_recording_replays_through_the_same_openai_contract(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.dumps({
                "id": "api-id", "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "same-agent-result",
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
    payload = {"model": "runtime-model", "messages": [{"role": "user", "content": "task"}]}
    api = ModelGateway(
        tmp_path / "api.json", mode="api",
        upstream_base_url=f"http://127.0.0.1:{server.server_port}/v1",
        upstream_api_key="key", upstream_model="recorded-model",
        request_namespace="api",
    )
    try:
        _status, _kind, api_body, request_id = api.complete_http(payload)
        api.mark_delivery(request_id, delivered=True)
        trace = tmp_path / "recorded.jsonl"
        api.write_replay_trace(trace)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    replay = ModelGateway(
        tmp_path / "replay.json", mode="replay", trace=trace,
        time_scale=0, request_namespace="replay",
    )
    _status, _kind, replay_body, replay_id = replay.complete_http(payload)
    replay.mark_delivery(replay_id, delivered=True)
    assert json.loads(api_body)["choices"][0]["message"] == (
        json.loads(replay_body)["choices"][0]["message"]
    )
    assert replay.replay_completeness()["complete"] is True
