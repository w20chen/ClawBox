from __future__ import annotations

import json
import os
import queue
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Any


@dataclass(frozen=True, slots=True)
class Ownership:
    run_id: str
    attempt_id: str
    task_uid: str
    experiment_id: str
    session_id: str
    policy_name: str

    def metadata(self) -> dict[str, str]:
        return {f"clawbox.{key.removesuffix('_id').replace('_', '-')}": value for key, value in asdict(self).items()}


@dataclass(frozen=True, slots=True)
class CubeSandboxTcpEndpoint:
    """Semantic raw TCP endpoint returned by CubeSandbox for one port."""

    sandbox_id: str
    container_port: int
    address: str


class OwnedSandboxJournal:
    """Durable primary cleanup index for one worker attempt."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()

    def record(self, sandbox_id: str, ownership: Ownership) -> None:
        record = {"sandbox_id": sandbox_id, "ownership": asdict(ownership)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    def sandbox_ids(self, *, task_uid: str | None = None) -> list[str]:
        if not self.path.exists():
            return []
        found: list[str] = []
        seen: set[str] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                sandbox_id = str(record["sandbox_id"])
                owner = record.get("ownership") or {}
            except (ValueError, KeyError, TypeError):
                continue
            if task_uid is not None and owner.get("task_uid") != task_uid:
                continue
            if sandbox_id not in seen:
                seen.add(sandbox_id)
                found.append(sandbox_id)
        return found


class CubeSandboxClient:
    """Small synchronous wrapper around the official CubeSandbox v0.7 SDK."""

    def __init__(self, *, journal: OwnedSandboxJournal | None = None,
                 sandbox_class: type | None = None, config: Any = None,
                 command_stream_grace_s: float = 15.0) -> None:
        if sandbox_class is None:
            from cubesandbox import Sandbox
            sandbox_class = Sandbox
        self._sandbox_class = sandbox_class
        self._config = config
        self.journal = journal
        self._handles: dict[str, Any] = {}
        if command_stream_grace_s < 0:
            raise ValueError("command_stream_grace_s must be non-negative")
        self.command_stream_grace_s = float(command_stream_grace_s)

    def _sdk_kwargs(self) -> dict[str, Any]:
        return {} if self._config is None else {"config": self._config}

    @staticmethod
    def sandbox_id(sandbox: Any) -> str:
        return str(sandbox.sandbox_id)

    def create_sandbox(self, *, template: str, node_name: str,
                       ownership: Ownership, env_vars: dict[str, str] | None = None,
                       allow_internet_access: bool = True,
                       network_allow_out: list[str] | None = None,
                       network_deny_out: list[str] | None = None) -> Any:
        network = {}
        if network_allow_out:
            network["allow_out"] = network_allow_out
        if network_deny_out:
            network["deny_out"] = network_deny_out
        sandbox = self._sandbox_class.create(
            template=template,
            timeout=-1,  # cubesandbox.NEVER_TIMEOUT in SDK v0.7.0
            lifecycle={"on_timeout": "kill", "auto_resume": False},
            metadata=ownership.metadata(),
            distribution_scope=[node_name],
            env_vars=env_vars,
            allow_internet_access=allow_internet_access,
            network=network or None,
            **self._sdk_kwargs(),
        )
        sandbox_id = self.sandbox_id(sandbox)
        self._handles[sandbox_id] = sandbox
        if self.journal is not None:
            self.journal.record(sandbox_id, ownership)
        return sandbox

    def connect_sandbox(self, sandbox_id: str) -> Any:
        sandbox = self._sandbox_class.connect(sandbox_id, **self._sdk_kwargs())
        self._handles[sandbox_id] = sandbox
        return sandbox

    def get_sandbox_state(self, sandbox_id: str) -> str | None:
        for info in self._sandbox_class.list_v2(**self._sdk_kwargs()):
            if self._info_id(info) == sandbox_id:
                return str(info.get("state", "")).lower() or None
        return None

    def get_tcp_endpoint(self, sandbox: Any, container_port: int = 2222) -> CubeSandboxTcpEndpoint:
        """Ask CubeSandbox for the raw TCP endpoint of a sandbox port.

        The ClawBox layer consumes only this semantic contract. It does not
        inspect CubeProxy Redis fields, guest IPs, or host-port metadata.
        """
        if isinstance(container_port, bool) or not isinstance(container_port, int):
            raise ValueError("container_port must be an integer between 1 and 65535")
        if not 1 <= container_port <= 65535:
            raise ValueError("container_port must be between 1 and 65535")
        getter = getattr(sandbox, "get_tcp_endpoint", None)
        if not callable(getter):
            raise RuntimeError(
                "CubeSandbox SDK must expose get_tcp_endpoint(container_port)"
            )
        raw = getter(container_port)
        if isinstance(raw, Mapping):
            sandbox_id = raw.get("sandboxID") or raw.get("sandbox_id")
            returned_port = raw.get("containerPort") or raw.get("container_port")
            address = raw.get("address")
        else:
            sandbox_id = getattr(raw, "sandbox_id", None)
            returned_port = getattr(raw, "container_port", None)
            address = getattr(raw, "address", None)
        endpoint = CubeSandboxTcpEndpoint(
            sandbox_id=str(sandbox_id or ""),
            container_port=int(returned_port or 0),
            address=str(address or "").strip(),
        )
        expected_id = self.sandbox_id(sandbox)
        if endpoint.sandbox_id != expected_id:
            raise RuntimeError(
                f"CubeSandbox endpoint identity mismatch: expected {expected_id!r}, "
                f"got {endpoint.sandbox_id!r}"
            )
        if endpoint.container_port != container_port:
            raise RuntimeError(
                f"CubeSandbox endpoint port mismatch: expected {container_port}, "
                f"got {endpoint.container_port}"
            )
        if not endpoint.address:
            raise RuntimeError("CubeSandbox returned an empty raw TCP endpoint")
        return endpoint

    def run_command(self, sandbox: Any, command: str, *, timeout_s: float,
                    cwd: str = "/workspace") -> Any:
        """Run a command without allowing a broken stream to block forever.

        CubeSandbox's e2b-connect transport intentionally leaves stream reads
        unbounded and relies on the server-side command deadline.  A proxy or
        a partially closed stream can therefore outlive that deadline.  Keep
        the SDK call in a daemon thread and enforce the same deadline locally,
        with a short grace period for normal deadline propagation.  The owning
        session fails and its normal cleanup path kills the sandbox; the
        daemon thread is only retained for the broken transport edge case and
        cannot keep the worker process alive.
        """
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result_queue.put(("result", sandbox.commands.run(
                    command, timeout=timeout_s, cwd=cwd,
                )))
            except BaseException as exc:  # re-raise in the caller's context
                result_queue.put(("error", exc))

        thread = threading.Thread(target=invoke, name="cube-command", daemon=True)
        thread.start()
        thread.join(timeout_s + self.command_stream_grace_s)
        if thread.is_alive():
            raise TimeoutError(
                "CubeSandbox command stream exceeded deadline "
                f"({timeout_s:g}s + {self.command_stream_grace_s:g}s grace)"
            )
        kind, value = result_queue.get_nowait()
        if kind == "error":
            raise value
        return value

    @staticmethod
    def read_file(sandbox: Any, path: str) -> str:
        return sandbox.files.read(path)

    @staticmethod
    def write_file(sandbox: Any, path: str, data: str | bytes) -> None:
        sandbox.files.write(path, data)

    @staticmethod
    def pause_sandbox(sandbox: Any) -> None:
        sandbox.pause(wait=True)

    def kill_sandbox(self, sandbox_or_id: Any) -> None:
        if not isinstance(sandbox_or_id, str):
            sandbox_or_id.kill()
            self._handles.pop(self.sandbox_id(sandbox_or_id), None)
            return
        sandbox_id = sandbox_or_id
        handle = self._handles.pop(sandbox_id, None)
        if handle is None:
            info = next((item for item in self._sandbox_class.list_v2(**self._sdk_kwargs())
                         if self._info_id(item) == sandbox_id), None)
            if info is None:
                return
            # Constructing an SDK handle from list data lets kill use the official
            # SDK without connect(), which would unnecessarily resume a paused VM.
            handle = self._sandbox_class(info, self._config)
        handle.kill()

    def get_sandbox_metrics(self, sandbox: Any) -> dict[str, Any] | None:
        getter = getattr(sandbox, "get_metrics", None)
        return None if getter is None else getter()

    def list_owned_sandboxes(self, owner_id: str) -> list[dict[str, Any]]:
        return [item for item in self._sandbox_class.list_v2(**self._sdk_kwargs())
                if (item.get("metadata") or {}).get("clawbox.task-uid") == owner_id]

    def kill_owned_sandboxes(self, owner_id: str) -> None:
        ids = self.journal.sandbox_ids(task_uid=owner_id) if self.journal else []
        ids.extend(self._info_id(item) for item in self.list_owned_sandboxes(owner_id))
        for sandbox_id in dict.fromkeys(filter(None, ids)):
            self.kill_sandbox(sandbox_id)
        remaining = self.list_owned_sandboxes(owner_id)
        if remaining:
            raise RuntimeError(f"CubeSandbox cleanup incomplete for task {owner_id}: "
                               f"{[self._info_id(item) for item in remaining]}")

    @staticmethod
    def _info_id(info: dict[str, Any]) -> str:
        return str(info.get("sandboxID") or info.get("sandbox_id") or "")
