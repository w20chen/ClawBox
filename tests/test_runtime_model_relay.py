import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from clawbox.experiments.runtime_model_relay import RUNTIME_MODEL_RELAY_SCRIPT


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _post(url: str, body: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.read()


def test_runtime_relay_reissues_pending_request_after_checkpoint(tmp_path: Path) -> None:
    attempts: list[str] = []
    first_started = threading.Event()

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            attempts.append(self.headers.get("Authorization", ""))
            if len(attempts) == 1:
                first_started.set()
                threading.Event().wait(30)
                return
            body = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    relay_port = _free_port()
    script = tmp_path / "relay.py"
    script.write_text(RUNTIME_MODEL_RELAY_SCRIPT, encoding="utf-8")
    environment = dict(os.environ)
    environment.update({
        "CLAWBOX_RELAY_UPSTREAM": f"http://127.0.0.1:{upstream.server_port}/v1",
        "CLAWBOX_RELAY_TOKEN": "session-token",
        "CLAWBOX_RELAY_PORT": str(relay_port),
    })
    relay = subprocess.Popen(
        [sys.executable, str(script)], env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{relay_port}/healthz", timeout=0.2,
                ).close()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(
                _post, f"http://127.0.0.1:{relay_port}/v1/chat/completions",
                {"model": "test", "messages": [], "stream": True},
            )
            assert first_started.wait(2)
            status, checkpoint_body = _post(
                f"http://127.0.0.1:{relay_port}/checkpoint", {},
            )
            assert status == 200
            assert json.loads(checkpoint_body)["retried"] == 1
            status, body = response.result(timeout=3)
        assert status == 200
        assert body.endswith(b"data: [DONE]\n\n")
        assert attempts == ["Bearer session-token", "Bearer session-token"]
    finally:
        relay.terminate()
        relay.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)
