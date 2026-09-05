"""Synchronous control plane for native OpenClaw SSH tool executions.

Only admission/completion metadata crosses this HTTP boundary.  Commands,
stdin, stdout, stderr, and workspace contents remain on the Runtime -> Tool
SSH data path.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


_IDENTITY = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class SessionLifecycle(str, Enum):
    ACTIVE = "active"
    DRAINING = "draining"
    CLOSED = "closed"


@dataclass(slots=True)
class _Execution:
    request: dict[str, Any]
    admission: dict[str, Any] | None = None
    completion: dict[str, Any] | None = None
    admitting: bool = False
    completing: bool = False
    admission_started_monotonic_s: float | None = None
    admission_completed_monotonic_s: float | None = None
    completion_started_monotonic_s: float | None = None
    completion_completed_monotonic_s: float | None = None


@dataclass(slots=True)
class _Session:
    session_id: str
    token: str
    admit: Callable[[dict[str, Any]], dict[str, Any]]
    complete: Callable[[dict[str, Any]], dict[str, Any]]
    lifecycle: SessionLifecycle = SessionLifecycle.ACTIVE
    executions: dict[str, _Execution] = field(default_factory=dict)
    condition: threading.Condition = field(default_factory=threading.Condition)

    def admission(self, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Return an idempotent admission; the callback runs exactly once."""
        execution_id = request["execution_id"]
        with self.condition:
            if self.lifecycle is not SessionLifecycle.ACTIVE:
                raise RuntimeError(f"session is {self.lifecycle.value}")
            execution = self.executions.get(execution_id)
            if execution is None:
                execution = _Execution(
                    request=dict(request), admitting=True,
                    admission_started_monotonic_s=time.monotonic(),
                )
                self.executions[execution_id] = execution
                owner = True
            else:
                if execution.request["command_sha256"] != request["command_sha256"]:
                    raise ValueError("execution_id was reused for a different command")
                owner = False
                while execution.admitting:
                    self.condition.wait()
                if execution.admission is None:
                    raise RuntimeError("prior admission attempt failed")
                return dict(execution.admission), True

        if owner:
            try:
                response = dict(self.admit(dict(request)))
                response.setdefault("decision", "ADMIT")
                if response["decision"] != "ADMIT":
                    raise RuntimeError("admission callback must block or return ADMIT")
            except Exception:
                with self.condition:
                    execution.admitting = False
                    self.condition.notify_all()
                raise
            with self.condition:
                execution.admission = response
                execution.admitting = False
                execution.admission_completed_monotonic_s = time.monotonic()
                self.condition.notify_all()
                return dict(response), False
        raise AssertionError("unreachable")

    def completion(self, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        execution_id = request["execution_id"]
        with self.condition:
            execution = self.executions.get(execution_id)
            if execution is None or execution.admission is None:
                raise ValueError("completion has no admitted execution")
            if execution.request["command_sha256"] != request["command_sha256"]:
                raise ValueError("completion command does not match admission")
            while execution.completing:
                self.condition.wait()
            if execution.completion is not None:
                return dict(execution.completion), True
            # Reserve completion ownership before leaving the lock. A duplicate
            # completion can then wait without causing a second reservation release.
            execution.completing = True
            execution.completion_started_monotonic_s = time.monotonic()
        try:
            response = dict(self.complete(dict(request)))
            response.setdefault("status", "COMPLETED")
        except Exception:
            with self.condition:
                execution.completing = False
                self.condition.notify_all()
            raise
        with self.condition:
            execution.completion = response
            execution.completing = False
            execution.completion_completed_monotonic_s = time.monotonic()
            self.condition.notify_all()
            return dict(response), False

    def drain(self, timeout: float | None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.condition:
            if self.lifecycle is SessionLifecycle.CLOSED:
                return True
            self.lifecycle = SessionLifecycle.DRAINING
            while any(item.admission is not None and item.completion is None
                      for item in self.executions.values()):
                if deadline is None:
                    self.condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            self.lifecycle = SessionLifecycle.CLOSED
            return True


class PolicyControlSession:
    def __init__(self, server: "PolicyControlServer", state: _Session) -> None:
        self._server = server
        self._state = state
        self.session_id = state.session_id
        self.token = state.token
        self.url = server.url

    @property
    def lifecycle(self) -> SessionLifecycle:
        with self._state.condition:
            return self._state.lifecycle

    def close(self, *, timeout: float | None = None) -> bool:
        return self._server.unregister(self.token, timeout=timeout)

    def records(self) -> list[dict[str, Any]]:
        with self._state.condition:
            return [
                {
                    "request": dict(item.request),
                    "admission": dict(item.admission) if item.admission else None,
                    "completion": dict(item.completion) if item.completion else None,
                    "timing": {
                        "admission_started_monotonic_s": item.admission_started_monotonic_s,
                        "admission_completed_monotonic_s": item.admission_completed_monotonic_s,
                        "admission_service_seconds": (
                            None if item.admission_started_monotonic_s is None
                            or item.admission_completed_monotonic_s is None else
                            max(0.0, item.admission_completed_monotonic_s
                                - item.admission_started_monotonic_s)
                        ),
                        "completion_started_monotonic_s": item.completion_started_monotonic_s,
                        "completion_completed_monotonic_s": item.completion_completed_monotonic_s,
                        "completion_service_seconds": (
                            None if item.completion_started_monotonic_s is None
                            or item.completion_completed_monotonic_s is None else
                            max(0.0, item.completion_completed_monotonic_s
                                - item.completion_started_monotonic_s)
                        ),
                    },
                }
                for item in self._state.executions.values()
            ]


class PolicyControlServer:
    """Concurrent, per-session admission endpoint for native SSH hooks."""

    def __init__(self, *, advertise_host: str, advertised_port: int,
                 bind_host: str = "0.0.0.0", bind_port: int = 18080) -> None:
        self.advertise_host = advertise_host
        self.advertised_port = advertised_port
        self.bind_host = bind_host
        self.bind_port = bind_port
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._started = False
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                started = time.monotonic()
                record: dict[str, Any] = {"path": self.path, "request_start": started}
                state: _Session | None = None
                try:
                    if self.path not in {"/v1/tool/admit", "/v1/tool/complete"}:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                    state = owner._lookup(token)
                    if state is None:
                        self.send_error(HTTPStatus.UNAUTHORIZED)
                        return
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 2 or length > 65536:
                        raise ValueError("invalid request size")
                    body = json.loads(self.rfile.read(length))
                    owner._validate(body, state.session_id)
                    record.update(session_id=state.session_id,
                                  execution_id=body["execution_id"])
                    if self.path.endswith("/admit"):
                        response, duplicate = state.admission(body)
                    else:
                        response, duplicate = state.completion(body)
                    response["session_id"] = state.session_id
                    response["execution_id"] = body["execution_id"]
                    response["duplicate"] = duplicate
                    self._json(HTTPStatus.OK, response)
                    record.update(status=200, duplicate=duplicate)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    record["status"] = 400
                    self._json(HTTPStatus.BAD_REQUEST, {"error": f"{type(exc).__name__}: {exc}"})
                except RuntimeError as exc:
                    record["status"] = 409
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except Exception as exc:
                    record["status"] = 503
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                               {"error": f"{type(exc).__name__}: {exc}"})
                finally:
                    record["response_end"] = time.monotonic()
                    record["latency_seconds"] = record["response_end"] - started
                    with owner._lock:
                        owner.requests.append(record)

            def _json(self, status: int, value: dict[str, Any]) -> None:
                encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        class Server(ThreadingHTTPServer):
            request_queue_size = 256
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server((bind_host, bind_port), Handler)
        self.actual_port = int(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="policy-control", daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.advertise_host}:{self.advertised_port}"

    def _lookup(self, token: str) -> _Session | None:
        with self._lock:
            return self._sessions.get(token)

    @staticmethod
    def _validate(body: Any, session_id: str) -> None:
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        if body.get("session_id") != session_id:
            raise ValueError("wrong-session routing")
        if not _IDENTITY.fullmatch(str(body.get("execution_id") or "")):
            raise ValueError("invalid execution_id")
        if not _SHA256.fullmatch(str(body.get("command_sha256") or "")):
            raise ValueError("invalid command_sha256")

    def register(self, session_id: str, *,
                 admit: Callable[[dict[str, Any]], dict[str, Any]],
                 complete: Callable[[dict[str, Any]], dict[str, Any]]) -> PolicyControlSession:
        if not _IDENTITY.fullmatch(session_id):
            raise ValueError("invalid session_id")
        token = secrets.token_urlsafe(32)
        state = _Session(session_id, token, admit, complete)
        with self._lock:
            if not self._started:
                raise RuntimeError("PolicyControlServer is not started")
            self._sessions[token] = state
        return PolicyControlSession(self, state)

    def unregister(self, token: str, *, timeout: float | None = None) -> bool:
        state = self._lookup(token)
        if state is None:
            return True
        drained = state.drain(timeout)
        if drained:
            with self._lock:
                self._sessions.pop(token, None)
        return drained

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __enter__(self) -> "PolicyControlServer":
        if self.bind_port and self.actual_port != self.bind_port:
            raise RuntimeError("policy server did not bind its configured port")
        self._started = True
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        with self._lock:
            tokens = list(self._sessions)
        for token in tokens:
            self.unregister(token, timeout=30)
        self._started = False
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
