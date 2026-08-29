"""Small host-side inference service for guest-driven replay experiments."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class StoredRequest:
    request_id: str
    session_id: str
    content: str
    recorded_latency_ms: int
    ready: bool = False
    result: str = ""


class ReplayInferenceService:
    """Idempotent request-ID service; replace its worker with a GPU backend later."""

    def __init__(self, store_path: Path, *, time_scale: float = 1.0) -> None:
        if time_scale < 0:
            raise ValueError("time_scale must be non-negative")
        self.store_path = store_path
        self.time_scale = time_scale
        self._lock = threading.Lock()
        self._requests: dict[str, StoredRequest] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._load()

    def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        if self._server is not None:
            raise RuntimeError("inference service is already running")
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/replay/requests":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length))
                    request = service.submit(payload)
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                self._send(HTTPStatus.ACCEPTED, service._public(request))

            def do_GET(self) -> None:  # noqa: N802
                prefix = "/v1/replay/requests/"
                if not self.path.startswith(prefix):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                request = service.get(self.path[len(prefix):])
                if request is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send(HTTPStatus.OK, service._public(request))

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                encoded = json.dumps(payload, sort_keys=True).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        bound_host, bound_port = self._server.server_address[:2]
        return f"http://{bound_host}:{bound_port}"

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def submit(self, payload: dict[str, Any]) -> StoredRequest:
        request_id = _token(payload.get("request_id"), "request_id")
        session_id = _token(payload.get("session_id"), "session_id")
        content = str(payload.get("content", ""))
        latency = int(payload.get("recorded_latency_ms", 0))
        if latency < 0:
            raise ValueError("recorded_latency_ms must be non-negative")
        with self._lock:
            existing = self._requests.get(request_id)
            if existing is not None:
                if (existing.session_id, existing.content, existing.recorded_latency_ms) != (session_id, content, latency):
                    raise ValueError("request_id was reused with different input")
                return existing
            request = StoredRequest(request_id, session_id, content, latency)
            self._requests[request_id] = request
            self._persist()
        threading.Thread(target=self._complete_after_wait, args=(request_id,), daemon=True).start()
        return request

    def get(self, request_id: str) -> StoredRequest | None:
        with self._lock:
            return self._requests.get(request_id)

    def _complete_after_wait(self, request_id: str) -> None:
        with self._lock:
            request = self._requests[request_id]
            delay = request.recorded_latency_ms / 1000 * self.time_scale
        time.sleep(delay)
        with self._lock:
            request = self._requests[request_id]
            request.ready = True
            request.result = f"replayed:{request_id}"
            self._persist()

    def _load(self) -> None:
        if not self.store_path.exists():
            return
        raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        self._requests = {item["request_id"]: StoredRequest(**item) for item in raw}

    def _persist(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.store_path.with_name(self.store_path.name + ".next")
        temporary.write_text(json.dumps([asdict(item) for item in self._requests.values()], sort_keys=True), encoding="utf-8")
        temporary.replace(self.store_path)

    @staticmethod
    def _public(request: StoredRequest) -> dict[str, Any]:
        return {"request_id": request.request_id, "ready": request.ready, "result": request.result if request.ready else ""}


def _token(value: object, name: str) -> str:
    token = str(value or "")
    if not token or len(token) > 255 or any(not (char.isalnum() or char in "._:-") for char in token):
        raise ValueError(f"{name} must be a safe non-empty token")
    return token
