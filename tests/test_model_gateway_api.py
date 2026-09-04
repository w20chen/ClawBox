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
