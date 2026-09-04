"""Idempotent OpenAI-compatible gateway for in-guest OpenClaw experiments."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import httpx

from .trace import ReplayAction, load_trace


@dataclass(slots=True)
class GatewayRequest:
    request_id: str
    replay_index: int | None
    request_namespace: str = "default"
    request_fingerprint: str = ""
    ready: bool = False
    status_code: int = 0
    content_type: str = "application/json"
    response_b64: str = ""
    error: str = ""
    started_unix_s: float = 0.0
    model_generated_unix_s: float = 0.0
    response_released_unix_s: float = 0.0
    completed_unix_s: float = 0.0
    delivered_unix_s: float = 0.0
    request_payload: dict[str, Any] = field(default_factory=dict)
    replay_input_match: bool | None = None
    replay_input_match_mode: str | None = None
    replay_input_expected_sha256: str | None = None
    replay_input_actual_sha256: str | None = None
    admission: dict[str, Any] = field(default_factory=dict)
    http_attempts: int = 0
    reconnect_attempts: int = 0
    delivery_failures: int = 0
    delivered: bool = False
    production_attempts: int = 0


class ModelGateway:
    """Keep the guest API identical while switching replay and real upstreams."""

    def __init__(self, store_path: Path, *, mode: str, trace: Path | None = None,
                 time_scale: float = 1.0, upstream_base_url: str | None = None,
                 upstream_api_key: str | None = None, upstream_model: str | None = None,
                 timeout_s: float = 600.0,
                 request_namespace: str = "default",
                 on_request_started: Callable[[], None] | None = None,
                 before_response_ready: Callable[[int | None, dict[str, Any]], dict[str, Any]] | None = None) -> None:
        if mode not in {"replay", "api"}:
            raise ValueError("mode must be replay or api")
        if time_scale < 0:
            raise ValueError("time_scale must be non-negative")
        if mode == "replay" and trace is None:
            raise ValueError("replay mode requires a trace")
        if mode == "api" and (not upstream_base_url or not upstream_api_key or not upstream_model):
            raise ValueError("api mode requires upstream URL, key, and model")
        self.store_path = store_path
        self.mode = mode
        self.actions = [] if trace is None else [a for a in load_trace(trace) if a.kind == "llm"]
        self.time_scale = time_scale
        self.upstream_base_url = (upstream_base_url or "").rstrip("/")
        self.upstream_api_key = upstream_api_key or ""
        self.upstream_model = upstream_model or ""
        self.timeout_s = timeout_s
        self.request_namespace = str(request_namespace).strip()
        if not self.request_namespace:
            raise ValueError("request_namespace must not be empty")
        self.on_request_started = on_request_started
        self.before_response_ready = before_response_ready
        self._requests: dict[str, GatewayRequest] = {}
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, host: str, port: int = 18081) -> str:
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/healthz":
                    self._send(HTTPStatus.OK, "application/json", b'{"ok":true}')
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:  # noqa: N802
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                request_id: str | None = None
                try:
                    payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                    status, content_type, body, request_id = gateway._complete_with_identity(
                        payload, http_attempt=True,
                    )
                    self._send(status, content_type, body)
                    gateway.mark_delivery(request_id, delivered=True)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    print(
                        f"model gateway rejected request: {type(exc).__name__}: {exc}",
                        file=sys.stderr, flush=True,
                    )
                    self._send(HTTPStatus.BAD_REQUEST, "application/json", json.dumps({"error": str(exc)}).encode())
                except (BrokenPipeError, ConnectionResetError):
                    if request_id is not None:
                        gateway.mark_delivery(request_id, delivered=False)

            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://{host}:{self._server.server_port}/v1"

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def complete(self, payload: dict[str, Any]) -> tuple[int, str, bytes]:
        status, content_type, body, _request_id = self._complete_with_identity(
            payload, http_attempt=False,
        )
        return status, content_type, body

    def complete_http(self, payload: dict[str, Any]) -> tuple[int, str, bytes, str]:
        """Complete an HTTP request and retain its request identity."""
        return self._complete_with_identity(payload, http_attempt=True)

    def _complete_with_identity(
        self, payload: dict[str, Any], *, http_attempt: bool,
    ) -> tuple[int, str, bytes, str]:
        if not isinstance(payload.get("messages"), list):
            raise ValueError("messages must be an array")
        canonical = dict(payload)
        canonical.pop("model", None)
        fingerprint = hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        started_event: dict[str, Any] | None = None
        with self._changed:
            matching = [
                item for item in self._requests.values()
                if item.request_fingerprint == fingerprint
            ]
            # An undelivered response may be retried after a broken connection.
            # Once delivered, an identical payload is a new logical model step.
            request = matching[-1] if matching and not matching[-1].delivered else None
            if request is None:
                index = len(self._requests) if self.mode == "replay" else None
                if index is not None and index >= len(self.actions):
                    raise ValueError("OpenClaw made more model calls than the replay trace contains")
                replay_input_match = None
                replay_input_match_mode = None
                replay_input_expected_sha256 = None
                replay_input_actual_sha256 = None
                if index is not None:
                    expected = self.actions[index].input
                    if isinstance(expected, list):
                        actual_input = canonical.get("messages")
                    elif isinstance(expected, dict) and expected:
                        actual_input = canonical
                    else:
                        actual_input = None
                    if actual_input is not None:
                        expected_identity = _canonical_replay_input(expected)
                        actual_identity = _canonical_replay_input(actual_input)
                        replay_input_match = expected_identity == actual_identity
                        replay_input_match_mode = "volatile_fields_v1"
                        replay_input_expected_sha256 = _canonical_sha256(expected_identity)
                        replay_input_actual_sha256 = _canonical_sha256(actual_identity)
                    if replay_input_match is False:
                        rejection = self.store_path.with_name(
                            f"model-rejected-request-{index:04d}.json"
                        )
                        temporary = rejection.with_name(rejection.name + ".next")
                        temporary.write_text(json.dumps({
                            "actual": actual_input,
                            "actual_canonical": actual_identity,
                            "actual_sha256": replay_input_actual_sha256,
                            "expected": expected,
                            "expected_canonical": expected_identity,
                            "expected_sha256": replay_input_expected_sha256,
                            "model_step": index,
                        }, sort_keys=True))
                        temporary.replace(rejection)
                        raise ValueError(f"replay request diverged at model step {index}")
                occurrence = len(matching)
                request_id = hashlib.sha256(
                    f"{self.request_namespace}\0{fingerprint}\0{occurrence}".encode()
                ).hexdigest()
                request = GatewayRequest(
                    request_id, index, started_unix_s=time.time(),
                    request_namespace=self.request_namespace,
                    request_fingerprint=fingerprint,
                    request_payload=canonical,
                    replay_input_match=replay_input_match,
                    replay_input_match_mode=replay_input_match_mode,
                    replay_input_expected_sha256=replay_input_expected_sha256,
                    replay_input_actual_sha256=replay_input_actual_sha256,
                )
                self._requests[request_id] = request
                self._persist()
                started_event = {
                    "request_id": request_id,
                    "replay_index": index,
                    "request_started_at": request.started_unix_s,
                    "request_fingerprint": fingerprint,
                }
            else:
                request_id = request.request_id
            if http_attempt:
                request.http_attempts += 1
                if request.http_attempts > 1:
                    request.reconnect_attempts += 1
                self._persist()
        # Lifecycle notification is deliberately outside the gateway condition
        # lock. The callback must only enqueue work and return immediately.
        if started_event is not None:
            self._emit_request_started(started_event)
            threading.Thread(
                target=self._produce, args=(request_id, payload), daemon=True,
            ).start()
        with self._changed:
            request = self._requests[request_id]
            deadline = time.monotonic() + self.timeout_s
            while not request.ready:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("model gateway request timed out")
                self._changed.wait(timeout=remaining)
            if request.error:
                raise RuntimeError(request.error)
            return (
                request.status_code, request.content_type,
                base64.b64decode(request.response_b64), request_id,
            )

    def _emit_request_started(self, event: dict[str, Any]) -> None:
        callback = self.on_request_started
        if callback is None:
            return
        try:
            if len(inspect.signature(callback).parameters) == 0:
                callback()
            else:
                callback(dict(event))
        except Exception as exc:
            # Do not turn a lifecycle observer failure into an inference
            # failure. Preserve the error as non-sensitive provenance.
            with self._changed:
                request = self._requests.get(str(event["request_id"]))
                if request is not None:
                    request.admission["request_event_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    self._persist()

    def mark_delivery(self, request_id: str, *, delivered: bool) -> None:
        """Record whether a host response write survived a guest disconnect."""
        with self._changed:
            request = self._requests.get(request_id)
            if request is None:
                raise KeyError(f"unknown gateway request {request_id}")
            if delivered:
                request.delivered = True
                if request.delivered_unix_s <= 0:
                    request.delivered_unix_s = time.time()
            else:
                request.delivery_failures += 1
            self._persist()

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            records: list[dict[str, Any]] = []
            for item in self._requests.values():
                record = asdict(item)
                # Stable semantic names for campaign analysis; retain the
                # *_unix_s fields for compatibility with the original CLI.
                record.update({
                    "request_started_at": item.started_unix_s,
                    "model_generated_at": item.model_generated_unix_s,
                    "response_released_at": item.response_released_unix_s,
                    "response_delivered_at": item.delivered_unix_s,
                    "pure_model_latency_seconds": (
                        item.model_generated_unix_s - item.started_unix_s
                        if item.model_generated_unix_s else None
                    ),
                    "policy_induced_response_hold_seconds": (
                        item.response_released_unix_s - item.model_generated_unix_s
                        if item.response_released_unix_s and item.model_generated_unix_s else None
                    ),
                    "delivery_latency_seconds": (
                        item.delivered_unix_s - item.response_released_unix_s
                        if item.delivered_unix_s and item.response_released_unix_s else None
                    ),
                    "runtime_visible_model_wait_seconds": (
                        item.delivered_unix_s - item.started_unix_s
                        if item.delivered_unix_s else None
                    ),
                })
                records.append(record)
            return records

    def logical_model_steps(self) -> int:
        """Count accepted logical model requests, excluding HTTP retries."""
        with self._lock:
            return len(self._requests)

    def replay_completeness(self, *, require_delivery: bool = True) -> dict[str, Any]:
        """Return a strict, auditable completion verdict for this session."""
        records = self.records()
        expected = len(self.actions) if self.mode == "replay" else None
        failed_matches = [
            item for item in records
            if self.mode == "replay" and item.get("replay_input_match") is not True
        ]
        incomplete = [
            item["request_id"] for item in records
            if not item.get("ready") or item.get("error") or item.get("status_code") != 200
            or (require_delivery and not item.get("delivered"))
        ]
        consumed = (
            sorted(item["replay_index"] for item in records
                   if item.get("replay_index") is not None)
            if self.mode == "replay" else []
        )
        expected_indices = list(range(expected or 0))
        complete = (
            self.mode != "replay" or (
                len(records) == expected
                and consumed == expected_indices
                and not failed_matches
            )
        ) and not incomplete
        return {
            "mode": self.mode,
            "observed_logical_model_steps": len(records),
            "expected_replay_model_steps": expected,
            "replay_indices": consumed,
            "replay_entries_consumed_exactly_once": (
                self.mode != "replay" or consumed == expected_indices
            ),
            "canonical_request_matches": not failed_matches,
            "required_responses_delivered": not incomplete,
            "retry_http_attempts": sum(
                max(0, int(item.get("http_attempts", 0)) - 1) for item in records
            ),
            "missing_or_failed_request_ids": incomplete,
            "complete": complete,
        }

    def pending_records(self) -> list[dict[str, Any]]:
        """Return only fields needed by checkpoint scheduling.

        Copying every accumulated request payload at the 50 ms controller poll
        rate becomes quadratic in conversation length and can starve response
        admission at c40.  Scheduler decisions need no message content.
        """
        with self._lock:
            return [
                {
                    "request_id": item.request_id,
                    "replay_index": item.replay_index,
                    "started_unix_s": item.started_unix_s,
                    "ready": item.ready,
                }
                for item in self._requests.values()
                if not item.ready
            ]

    def _produce(self, request_id: str, payload: dict[str, Any]) -> None:
        with self._changed:
            self._requests[request_id].production_attempts += 1
            self._persist()
        try:
            if self.mode == "replay":
                request = self._requests[request_id]
                assert request.replay_index is not None
                action = self.actions[request.replay_index]
                time.sleep(action.duration_s * self.time_scale)
                status, content_type, body = _replay_response(action, bool(payload.get("stream")))
            else:
                forwarded = dict(payload)
                forwarded["model"] = self.upstream_model
                with httpx.Client(trust_env=False, timeout=self.timeout_s) as client:
                    response = client.post(
                        self.upstream_base_url + "/chat/completions",
                        headers={"Authorization": f"Bearer {self.upstream_api_key}"},
                        json=forwarded,
                    )
                status = response.status_code
                content_type = response.headers.get("content-type", "application/json")
                body = response.content
            # Capture the pure model/replay completion before any policy or
            # restore operation. ``completed_unix_s`` remains the legacy
            # response-release timestamp for old consumers.
            model_generated = time.time()
            with self._changed:
                request = self._requests[request_id]
                request.model_generated_unix_s = model_generated
                self._persist()
            admission = {}
            if self.before_response_ready is not None:
                event = {
                    "request_id": request_id,
                    "replay_index": self._requests[request_id].replay_index,
                    "request_started_at": self._requests[request_id].started_unix_s,
                    "model_generated_at": model_generated,
                }
                message = _response_message(content_type, body)
                callback = self.before_response_ready
                if len(inspect.signature(callback).parameters) >= 3:
                    admission = callback(
                        self._requests[request_id].replay_index, message, event,
                    )
                else:
                    admission = callback(
                        self._requests[request_id].replay_index, message,
                    )
            error = ""
        except Exception as exc:  # surfaced to the waiting guest request
            status, content_type, body, error, admission = 500, "application/json", b"", str(exc), {}
        with self._changed:
            request = self._requests[request_id]
            request.status_code = status
            request.content_type = content_type
            request.response_b64 = base64.b64encode(body).decode()
            request.error = error
            request.admission = admission
            request.ready = True
            request.response_released_unix_s = time.time()
            request.completed_unix_s = request.response_released_unix_s
            self._persist()
            self._changed.notify_all()

    def write_replay_trace(self, path: Path) -> None:
        """Export successful API responses as an ordered replayable v4 trace."""
        records = []
        for index, item in enumerate(self.records()):
            if not item["ready"] or item["error"] or item["status_code"] != 200:
                raise ValueError("cannot export an incomplete or failed model response")
            message = _response_message(
                item["content_type"], base64.b64decode(item["response_b64"])
            )
            start = float(item["started_unix_s"])
            end = float(item["completed_unix_s"])
            if start <= 0 or end < start:
                raise ValueError("model response has no valid timing metadata")
            records.append({
                "type": "action", "action_type": "llm_call",
                "action_id": f"model-{index + 1}", "iteration": index,
                "ts_start": start, "ts_end": end,
                "data": {"model": self.upstream_model or "recorded-model",
                         "raw_request": item["request_payload"],
                         "raw_response": message,
                         "llm_latency_ms": (end - start) * 1000.0},
            })
        temporary = path.with_name(path.name + ".next")
        temporary.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _persist(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.store_path.with_name(self.store_path.name + ".next")
        temporary.write_text(json.dumps([asdict(item) for item in self._requests.values()], sort_keys=True))
        temporary.replace(self.store_path)


_RUNTIME_SESSION_RE = re.compile(
    r"(?m)^(Runtime: .*?\| session=agent:main:explicit:)session-\d+"
    r"( \| sessionId=)session-\d+( \|.*)$"
)
_PYTEST_TIME_RE = re.compile(r"(?m)^(\d+ passed in )\d+(?:\.\d+)?s$")
_OPENCLAW_PROMPT_TIME_RE = re.compile(
    r"(?m)^\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d{2}-\d{2} "
    r"\d{2}:\d{2} UTC\](?= )"
)
_GENERATED_DIRECTORY_MTIME_RE = re.compile(
    r"(?m)^(.+\s)(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"\d{1,2} \d{2}:\d{2} "
    r"(\.|\.clawbox|openclaw-ssh-shared-[^\s]+)$"
)
_GIT_COMMIT_HEADER_RE = re.compile(r"(?m)^(\[master )[0-9a-f]{7,40}(\] )")
_GIT_LOG_HEAD_RE = re.compile(
    r"(?m)^[0-9a-f]{7,40}(?= .+\n(?:[0-9a-f]{7,40} |\?\? ))"
)


def _canonical_replay_text(value: str) -> str:
    """Mask only per-session values known to be nondeterministic in this workload."""
    value = _RUNTIME_SESSION_RE.sub(r"\1session-N\2session-N\3", value)
    value = _OPENCLAW_PROMPT_TIME_RE.sub("[REPLAY-TIME]", value)
    value = _GENERATED_DIRECTORY_MTIME_RE.sub(r"\1REPLAY-MTIME \2", value)
    value = _PYTEST_TIME_RE.sub(r"\1N.NNs", value)
    value = _GIT_COMMIT_HEADER_RE.sub(r"\1COMMIT\2", value)
    return _GIT_LOG_HEAD_RE.sub("COMMIT", value)


def _canonical_replay_input(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical_replay_input(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_replay_input(item) for item in value]
    if isinstance(value, str):
        return _canonical_replay_text(value)
    return value


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


def _replay_response(action: ReplayAction, stream: bool) -> tuple[int, str, bytes]:
    message = action.output
    if isinstance(message, dict) and set(message) == {"content"} and isinstance(message["content"], dict):
        message = message["content"]
    if isinstance(message, str):
        message = {"content": message}
    if not isinstance(message, dict):
        raise ValueError(f"replay action {action.action_id} has no OpenAI-compatible response")
    message = {"role": "assistant", **message}
    finish = "tool_calls" if message.get("tool_calls") else "stop"
    response_id = f"replay-{action.action_id}"
    if not stream:
        body = {"id": response_id, "object": "chat.completion", "created": 0,
                "model": action.name, "choices": [{"index": 0, "message": message,
                "finish_reason": finish}], "usage": {"prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0}}
        return 200, "application/json", json.dumps(body, separators=(",", ":")).encode()
    chunk = {"id": response_id, "object": "chat.completion.chunk", "created": 0,
             "model": action.name, "choices": [{"index": 0, "delta": message,
             "finish_reason": finish}]}
    body = f"data: {json.dumps(chunk, separators=(',', ':'))}\n\ndata: [DONE]\n\n".encode()
    return 200, "text/event-stream", body


def _response_message(content_type: str, body: bytes) -> dict[str, Any]:
    if "text/event-stream" not in content_type.lower():
        message = json.loads(body)["choices"][0]["message"]
        if not isinstance(message, dict):
            raise ValueError("model response message is not an object")
        return {key: value for key, value in message.items() if key != "role"}
    content: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    for raw_line in body.decode("utf-8").splitlines():
        if not raw_line.startswith("data:"):
            continue
        encoded = raw_line[5:].strip()
        if not encoded or encoded == "[DONE]":
            continue
        for choice in json.loads(encoded).get("choices", []):
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            for position, fragment in enumerate(delta.get("tool_calls") or []):
                # Recorded non-streaming responses contain complete tool calls
                # without the per-fragment ``index`` required by OpenAI's
                # streaming format.  _replay_response emits those complete
                # calls in one SSE delta, so preserve their array positions
                # instead of accidentally merging every call into slot zero.
                index = int(fragment.get("index", position))
                target = tool_calls.setdefault(
                    index, {"id": "", "type": "function",
                            "function": {"name": "", "arguments": ""}},
                )
                if fragment.get("id"):
                    target["id"] += str(fragment["id"])
                if fragment.get("type"):
                    target["type"] = fragment["type"]
                function = fragment.get("function") or {}
                target["function"]["name"] += str(function.get("name") or "")
                target["function"]["arguments"] += str(function.get("arguments") or "")
    message: dict[str, Any] = {"content": "".join(content)}
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    return message
