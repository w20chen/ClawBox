from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from enum import Enum
from threading import RLock
from typing import Mapping

from clawbox.replay.lifecycle import LifecycleError

from .client import CubeSandboxClient, Ownership


class SandboxState(str, Enum):
    NEW = "new"
    CREATING = "creating"
    RUNNING = "running"
    CHECKPOINTING = "checkpointing"
    SWAPPED = "swapped"
    RESTORING = "restoring"
    DESTROYING = "destroying"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class LifecycleTiming:
    operation: str
    started_unix_s: float
    completed_unix_s: float
    started_monotonic_s: float
    completed_monotonic_s: float
    service_seconds: float
    state_before: str
    state_after: str
    status: str = "ok"
    error_type: str | None = None


class CubeSandboxLifecycle:
    """Concurrency-safe policy layer over official CubeSandbox lifecycle calls."""

    def __init__(self, client: CubeSandboxClient, *, template: str,
                 node_name: str, ownership: Ownership,
                 allow_internet_access: bool = True,
                 env_vars: Mapping[str, str] | None = None,
                 network_allow_out: list[str] | None = None,
                 network_deny_out: list[str] | None = None) -> None:
        self.client = client
        self.template = template
        self.node_name = node_name
        self.ownership = ownership
        self.allow_internet_access = allow_internet_access
        self.env_vars = dict(env_vars or {})
        self.network_allow_out = list(network_allow_out or [])
        self.network_deny_out = list(network_deny_out or [])
        self.sandbox = None
        self.sandbox_id: str | None = None
        self._state = SandboxState.NEW
        self._lock = RLock()
        self._timings: list[LifecycleTiming] = []

    @property
    def state(self) -> SandboxState:
        with self._lock:
            return self._state

    @property
    def resident(self) -> bool:
        return self.state is SandboxState.RUNNING

    @property
    def timings(self) -> list[dict[str, object]]:
        with self._lock:
            return [asdict(item) for item in self._timings]

    def _record(self, operation: str, before: SandboxState, after: SandboxState,
                started_wall: float, started_mono: float, *,
                status: str = "ok", error_type: str | None = None) -> float:
        completed_wall = time.time()
        completed_mono = time.monotonic()
        duration = max(0.0, completed_mono - started_mono)
        self._timings.append(LifecycleTiming(
            operation, started_wall, completed_wall, started_mono,
            completed_mono, duration, before.value, after.value,
            status, error_type,
        ))
        return duration

    def start(self) -> float:
        with self._lock:
            if self._state is not SandboxState.NEW:
                raise LifecycleError("CubeSandbox lifecycle already started")
            before = self._state
            self._state = SandboxState.CREATING
            started_wall, started_mono = time.time(), time.monotonic()
            try:
                self.sandbox = self.client.create_sandbox(
                    template=self.template, node_name=self.node_name,
                    ownership=self.ownership, env_vars=self.env_vars or None,
                    allow_internet_access=self.allow_internet_access,
                    network_allow_out=self.network_allow_out or None,
                    network_deny_out=self.network_deny_out or None,
                )
                self.sandbox_id = self.client.sandbox_id(self.sandbox)
                self._state = SandboxState.RUNNING
                return self._record("create", before, self._state,
                                    started_wall, started_mono)
            except Exception as exc:
                self._state = before
                self._record(
                    "create", before, before, started_wall, started_mono,
                    status="error", error_type=type(exc).__name__,
                )
                raise

    def checkpoint_and_evict(self) -> float:
        with self._lock:
            if self.sandbox is None or self._state is not SandboxState.RUNNING:
                raise LifecycleError("CubeSandbox is not resident")
            before = self._state
            self._state = SandboxState.CHECKPOINTING
            started_wall, started_mono = time.time(), time.monotonic()
            try:
                self.client.pause_sandbox(self.sandbox)
                self._state = SandboxState.SWAPPED
                return self._record("checkpoint", before, self._state,
                                    started_wall, started_mono)
            except Exception as exc:
                self._state = before
                self._record(
                    "checkpoint", before, before, started_wall, started_mono,
                    status="error", error_type=type(exc).__name__,
                )
                raise

    pause_and_evict = checkpoint_and_evict

    def restore(self) -> float:
        with self._lock:
            if self.sandbox_id is None or self._state is not SandboxState.SWAPPED:
                raise LifecycleError("CubeSandbox is not swapped out")
            before = self._state
            self._state = SandboxState.RESTORING
            started_wall, started_mono = time.time(), time.monotonic()
            try:
                self.sandbox = self.client.connect_sandbox(self.sandbox_id)
                self._state = SandboxState.RUNNING
                return self._record("restore", before, self._state,
                                    started_wall, started_mono)
            except Exception as exc:
                self._state = before
                self._record(
                    "restore", before, before, started_wall, started_mono,
                    status="error", error_type=type(exc).__name__,
                )
                raise

    def ensure_network_allow_out(self, cidr: str) -> bool:
        """Allow one additional destination CIDR, updating a running VM.

        CubeSandbox treats an update as a replacement policy.  Keep the
        complete desired policy on this lifecycle object and send it only when
        the new CIDR is not already present.  This lets endpoint ports change
        freely while still allowing a Tool to move to a different
        deployment-owned endpoint host after restore.
        """
        value = str(cidr or "").strip()
        if not value:
            raise ValueError("network allowlist CIDR must not be empty")
        with self._lock:
            if value in self.network_allow_out:
                return False
            desired = [*self.network_allow_out, value]
            if self._state is SandboxState.RUNNING:
                before = self._state
                started_wall, started_mono = time.time(), time.monotonic()
                try:
                    self.client.update_network(
                        self.sandbox, allow_internet_access=self.allow_internet_access,
                        network_allow_out=desired,
                        network_deny_out=self.network_deny_out,
                    )
                except Exception as exc:
                    self._record(
                        "network_update", before, before, started_wall, started_mono,
                        status="error", error_type=type(exc).__name__,
                    )
                    raise
                self._record("network_update", before, self._state,
                             started_wall, started_mono)
            self.network_allow_out = desired
            return True

    def close(self) -> float:
        with self._lock:
            if self._state is SandboxState.CLOSED:
                return 0.0
            before = self._state
            self._state = SandboxState.DESTROYING
            started_wall, started_mono = time.time(), time.monotonic()
            try:
                if self.sandbox_id is not None:
                    self.client.kill_sandbox(self.sandbox_id)
                self.sandbox = None
                self.sandbox_id = None
                self._state = SandboxState.CLOSED
                return self._record("destroy", before, self._state,
                                    started_wall, started_mono)
            except Exception as exc:
                self._state = before
                self._record(
                    "destroy", before, before, started_wall, started_mono,
                    status="error", error_type=type(exc).__name__,
                )
                raise
