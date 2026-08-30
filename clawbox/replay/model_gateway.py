"""Idempotent OpenAI-compatible gateway for in-guest OpenClaw experiments."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from .trace import ReplayAction, load_trace


@dataclass(slots=True)
class GatewayRequest:
    request_id: str
    replay_index: int | None
    ready: bool = False
    status_code: int = 0
    content_type: str = "application/json"
    response_b64: str = ""
    error: str = ""
    started_unix_s: float = 0.0
    completed_unix_s: float = 0.0


class ModelGateway:
    """Keep the guest API identical while switching replay and real upstreams."""

    def __init__(self, store_path: Path, *, mode: str, trace: Path | None = None,
                 time_scale: float = 1.0, upstream_base_url: str | None = None,
                 upstream_api_key: str | None = None, upstream_model: str | None = None,
                 timeout_s: float = 600.0) -> None:
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
                try:
                    payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                    status, content_type, body = gateway.complete(payload)
                    self._send(status, content_type, body)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._send(HTTPStatus.BAD_REQUEST, "application/json", json.dumps({"error": str(exc)}).encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass

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
        if not isinstance(payload.get("messages"), list):
            raise ValueError("messages must be an array")
        canonical = dict(payload)
        canonical.pop("model", None)
        request_id = hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        with self._changed:
            request = self._requests.get(request_id)
            if request is None:
                index = len(self._requests) if self.mode == "replay" else None
                if index is not None and index >= len(self.actions):
                    raise ValueError("OpenClaw made more model calls than the replay trace contains")
                request = GatewayRequest(request_id, index, started_unix_s=time.time())
                self._requests[request_id] = request
                self._persist()
                threading.Thread(
                    target=self._produce, args=(request_id, payload), daemon=True,
                ).start()
            deadline = time.monotonic() + self.timeout_s
            while not request.ready:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("model gateway request timed out")
                self._changed.wait(timeout=remaining)
            if request.error:
                raise RuntimeError(request.error)
            return request.status_code, request.content_type, base64.b64decode(request.response_b64)

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(item) for item in self._requests.values()]

    def _produce(self, request_id: str, payload: dict[str, Any]) -> None:
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
            error = ""
        except Exception as exc:  # surfaced to the waiting guest request
            status, content_type, body, error = 500, "application/json", b"", str(exc)
        with self._changed:
            request = self._requests[request_id]
            request.status_code = status
            request.content_type = content_type
            request.response_b64 = base64.b64encode(body).decode()
            request.error = error
            request.ready = True
            request.completed_unix_s = time.time()
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
            for fragment in delta.get("tool_calls") or []:
                index = int(fragment.get("index", 0))
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
