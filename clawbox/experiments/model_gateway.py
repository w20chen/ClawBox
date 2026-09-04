"""Managed, node-routed OpenAI gateway with session-local replay state.

The Worker owns the model boundary. Runtime guests authenticate with a
per-session bearer token and never receive the upstream API credential. One
fixed listener serves all sessions; every replay cursor, idempotency ledger,
request record, and completion check lives in its own ``SessionGatewayState``.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from clawbox.replay.model_gateway import ModelGateway


class SessionGatewayState:
    """Independent gateway state for exactly one Runtime session."""

    def __init__(self, *, session_id: str, token: str, store_path: Path,
                 mode: str, trace: Path | None, time_scale: float,
                 upstream_base_url: str | None, upstream_api_key: str | None,
                 upstream_model: str | None,
                 on_request_started: Callable[..., None] | None = None,
                 before_response_ready: Callable[..., dict[str, Any]] | None = None) -> None:
        self.session_id = session_id
        self.token = token
        self.url = ""
        self.gateway = ModelGateway(
            store_path, mode=mode, trace=trace, time_scale=time_scale,
            upstream_base_url=upstream_base_url,
            upstream_api_key=upstream_api_key,
            upstream_model=upstream_model,
            request_namespace=session_id,
            on_request_started=on_request_started,
            before_response_ready=before_response_ready,
        )
        self.created_unix_s = time.time()
        self._condition = threading.Condition()
        self._active_requests = 0
        self._draining = False

    @property
    def mode(self) -> str:
        return self.gateway.mode

    @property
    def actions(self) -> list[Any]:
        return self.gateway.actions

    def acquire(self) -> bool:
        with self._condition:
            if self._draining:
                return False
            self._active_requests += 1
            return True

    def release(self) -> None:
        with self._condition:
            self._active_requests -= 1
            if self._active_requests == 0:
                self._condition.notify_all()

    def drain(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            self._draining = True
            while self._active_requests:
                if deadline is None:
                    self._condition.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(remaining)
            return True

    def complete_http(self, payload: dict[str, Any]) -> tuple[int, str, bytes, str]:
        return self.gateway.complete_http(payload)

    def mark_delivery(self, request_id: str, *, delivered: bool) -> None:
        self.gateway.mark_delivery(request_id, delivered=delivered)

    def records(self) -> list[dict[str, Any]]:
        return self.gateway.records()

    def replay_completeness(self) -> dict[str, Any]:
        return self.gateway.replay_completeness(require_delivery=True)

    def write_replay_trace(self, path: Path) -> None:
        self.gateway.write_replay_trace(path)


class ManagedModelGateway:
    """One fixed node-routed HTTP listener dispatching session tokens."""

    def __init__(self, *, advertise_host: str, advertised_port: int,
                 bind_host: str = "0.0.0.0", bind_port: int = 18081) -> None:
        self.bind_host = bind_host
        self.advertised_host = advertise_host
        self.bind_port = int(bind_port)
        self.advertised_port = int(advertised_port)
        self._sessions: dict[str, SessionGatewayState] = {}
        self._lock = threading.RLock()
        self._started = False
        gateway = self

        class ConcurrentHTTPServer(ThreadingHTTPServer):
            request_queue_size = 256
            daemon_threads = True
            allow_reuse_address = True

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/healthz":
                    self._send(HTTPStatus.OK, b'{"ok":true}')
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:  # noqa: N802
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                authorization = self.headers.get("Authorization", "")
                if not authorization.startswith("Bearer "):
                    self.send_error(HTTPStatus.UNAUTHORIZED)
                    return
                state = gateway._acquire(authorization.removeprefix("Bearer "))
                if state is None:
                    self.send_error(HTTPStatus.UNAUTHORIZED)
                    return
                request_id: str | None = None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 8 * 1024 * 1024:
                        raise ValueError("invalid request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("request body must be an object")
                    status, content_type, body, request_id = state.complete_http(payload)
                    self._send(status, body, content_type=content_type)
                    state.mark_delivery(request_id, delivered=True)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._send(HTTPStatus.BAD_REQUEST, json.dumps({
                        "error": str(exc),
                    }).encode())
                except (BrokenPipeError, ConnectionResetError):
                    if request_id is not None:
                        state.mark_delivery(request_id, delivered=False)
                except Exception as exc:
                    # Keep credentials, prompts, and upstream response bodies
                    # out of logs and error text. The session store retains the
                    # gateway's precise non-secret error provenance.
                    self._send(HTTPStatus.BAD_GATEWAY, json.dumps({
                        "error": f"{type(exc).__name__}: {exc}",
                    }).encode())
                finally:
                    state.release()

            def _send(self, status: int, body: bytes,
                      *, content_type: str = "application/json") -> None:
                self.send_response(int(status))
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ConcurrentHTTPServer((self.bind_host, self.bind_port), Handler)
        self.actual_port = int(self.server.server_port)
        if self.advertised_port == 0:
            self.advertised_port = self.actual_port
        self.startup = {
            "bind_host": self.bind_host,
            "bind_port": self.actual_port,
            "advertised_host": self.advertised_host,
            "advertised_port": self.advertised_port,
            "url": self.url,
        }
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="model-gateway", daemon=True,
        )

    @property
    def url(self) -> str:
        return f"http://{self.advertised_host}:{self.advertised_port}/v1"

    def __enter__(self) -> "ManagedModelGateway":
        if self.bind_port and self.actual_port != self.bind_port:
            raise RuntimeError(
                f"managed ModelGateway must bind fixed port {self.bind_port}, "
                f"got {self.actual_port}"
            )
        self.thread.start()
        self._started = True
        return self

    def __exit__(self, *_args: object) -> None:
        with self._lock:
            tokens = list(self._sessions)
            self._started = False
        for token in tokens:
            self.unregister(token, timeout=30)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def url_without_version(self) -> str:
        return f"http://{self.advertised_host}:{self.advertised_port}"

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _acquire(self, token: str) -> SessionGatewayState | None:
        # The global lock is held only for lookup. Per-session model waits and
        # persistence happen after this method returns.
        with self._lock:
            state = self._sessions.get(token)
        if state is None or not state.acquire():
            return None
        return state

    def register(self, *, session_id: str, store_path: Path, mode: str,
                 trace: Path | None = None, time_scale: float = 1.0,
                 upstream_base_url: str | None = None,
                 upstream_api_key: str | None = None,
                 upstream_model: str | None = None,
                 on_request_started: Callable[..., None] | None = None,
                 before_response_ready: Callable[..., dict[str, Any]] | None = None,
                 ) -> SessionGatewayState:
        if not self._started:
            raise RuntimeError("managed ModelGateway must be started before registering")
        if not session_id:
            raise ValueError("session_id is required")
        token = secrets.token_urlsafe(32)
        state = SessionGatewayState(
            session_id=session_id, token=token, store_path=store_path,
            mode=mode, trace=trace, time_scale=time_scale,
            upstream_base_url=upstream_base_url,
            upstream_api_key=upstream_api_key,
            upstream_model=upstream_model,
            on_request_started=on_request_started,
            before_response_ready=before_response_ready,
        )
        state.url = self.url
        with self._lock:
            self._sessions[token] = state
        return state

    def unregister(self, token: str, *, timeout: float | None = None) -> bool:
        with self._lock:
            state = self._sessions.pop(token, None)
        return True if state is None else state.drain(timeout)

    def wait_ready(self, *, attempts: int = 8, initial_delay: float = 0.25) -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(self.url_without_version + "/healthz", method="GET")
        delay = initial_delay
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                with opener.open(request, timeout=5) as response:
                    if response.status == HTTPStatus.OK:
                        return
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
        raise RuntimeError(f"ModelGateway readiness failed for {self.url}: {last_error}")
