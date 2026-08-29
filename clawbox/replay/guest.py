from __future__ import annotations

import json
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from .lifecycle import LifecycleError


_TOKEN = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,255}$")


def _protocol_token(value: str, *, name: str, max_length: int = 255) -> str:
    if len(value) > max_length or _TOKEN.fullmatch(value) is None:
        raise ValueError(
            f"{name} must be a non-empty protocol token containing only "
            "letters, digits, or ._:/@+-"
        )
    return value


@dataclass(frozen=True, slots=True)
class RuntimeAgentState:
    boot_nonce: int
    turn: int
    tool_count: int
    inflight_request: str | None
    predicted_ms: int
    gpu_id: str
    kv_bytes: int
    resident_bytes: int

    @classmethod
    def from_response(cls, response: dict[str, Any]) -> "RuntimeAgentState":
        state = response.get("state")
        if not isinstance(state, dict):
            raise LifecycleError(f"guest response has no state: {response!r}")
        inflight = state.get("inflight_request")
        return cls(
            boot_nonce=int(state["boot_nonce"]),
            turn=int(state["turn"]),
            tool_count=int(state["tool_count"]),
            inflight_request=None if inflight in {None, ""} else str(inflight),
            predicted_ms=int(state.get("predicted_ms", 0)),
            gpu_id=str(state.get("gpu_id", "")),
            kv_bytes=int(state.get("kv_bytes", 0)),
            resident_bytes=int(state.get("resident_bytes", 0)),
        )


class RuntimeAgent(Protocol):
    def wait_ready(self, timeout_s: float) -> float: ...
    def state(self) -> RuntimeAgentState: ...
    def begin_llm(self, request_id: str, predicted_s: float,
                  metadata: dict[str, Any]) -> RuntimeAgentState: ...
    def assert_inflight(self, request_id: str,
                        expected_boot_nonce: int) -> RuntimeAgentState: ...
    def complete_llm(self, request_id: str) -> RuntimeAgentState: ...
    def tool_completed(self, action_id: str, exit_code: int) -> RuntimeAgentState: ...


class VsockRuntimeAgentClient:
    """Reconnect-per-command client for the stateful in-guest runtime agent."""

    def __init__(self, uds_path: Path, *, port: int = 18080,
                 timeout_s: float = 5.0) -> None:
        self.uds_path = uds_path
        self.port = port
        self.timeout_s = timeout_s
        self._lock = Lock()

    def wait_ready(self, timeout_s: float) -> float:
        started = time.monotonic()
        deadline = started + timeout_s
        last_error = "guest agent did not answer"
        while time.monotonic() < deadline:
            try:
                self._request("PING", timeout_s=min(self.timeout_s, max(0.1, deadline - time.monotonic())))
                return time.monotonic() - started
            except (OSError, LifecycleError) as exc:
                last_error = str(exc)
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        raise LifecycleError(f"runtime guest agent did not become ready: {last_error}")

    def state(self) -> RuntimeAgentState:
        return RuntimeAgentState.from_response(self._request("STATE"))

    def begin_llm(self, request_id: str, predicted_s: float,
                  metadata: dict[str, Any]) -> RuntimeAgentState:
        request_id = _protocol_token(request_id, name="request_id")
        gpu_id = _protocol_token(
            str(metadata.get("gpu_id") or "none"), name="gpu_id", max_length=127,
        )
        kv_bytes = int(metadata.get("kv_bytes") or 0)
        if kv_bytes < 0:
            raise ValueError("kv_bytes must be non-negative")
        command = f"BEGIN {request_id} {max(0, round(predicted_s * 1000))} {gpu_id} {kv_bytes}"
        return RuntimeAgentState.from_response(self._request(command))

    def assert_inflight(self, request_id: str,
                        expected_boot_nonce: int) -> RuntimeAgentState:
        request_id = _protocol_token(request_id, name="request_id")
        state = self.state()
        if state.boot_nonce != expected_boot_nonce:
            raise LifecycleError(
                "guest cold-booted instead of restoring: "
                f"expected nonce {expected_boot_nonce}, got {state.boot_nonce}"
            )
        if state.inflight_request != request_id:
            raise LifecycleError(
                f"guest lost in-flight request {request_id!r}: {state.inflight_request!r}"
            )
        return state

    def complete_llm(self, request_id: str) -> RuntimeAgentState:
        request_id = _protocol_token(request_id, name="request_id")
        return RuntimeAgentState.from_response(self._request(f"COMPLETE {request_id}"))

    def tool_completed(self, action_id: str, exit_code: int) -> RuntimeAgentState:
        action_id = _protocol_token(action_id, name="action_id")
        return RuntimeAgentState.from_response(self._request(f"TOOL {action_id} {exit_code}"))

    def _request(self, command: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        if "\n" in command or "\r" in command:
            raise ValueError("guest command must be one line")
        timeout = self.timeout_s if timeout_s is None else timeout_s
        with self._lock, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(self.uds_path))
            client.sendall(f"CONNECT {self.port}\n".encode("ascii"))
            acknowledgement = self._readline(client, 128)
            if not acknowledgement.startswith(b"OK "):
                raise LifecycleError(f"Firecracker vsock handshake failed: {acknowledgement!r}")
            client.sendall(command.encode("utf-8") + b"\n")
            raw = self._readline(client, 65536)
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LifecycleError(f"invalid guest response: {raw[:200]!r}") from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise LifecycleError(f"guest command failed: {response!r}")
        return response

    @staticmethod
    def _readline(client: socket.socket, limit: int) -> bytes:
        data = bytearray()
        while len(data) < limit:
            chunk = client.recv(1)
            if not chunk:
                break
            if chunk == b"\n":
                return bytes(data)
            data.extend(chunk)
        raise LifecycleError(f"unterminated or oversized guest response ({len(data)} bytes)")
