from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from threading import Condition
from typing import Protocol

from .spec import EvictionPolicy, PolicySpec, ReclamationPolicy, RestorePolicy

MIB = 1024 * 1024


class Pausable(Protocol):
    @property
    def resident(self) -> bool: ...
    def checkpoint_and_evict(self) -> float: ...


@dataclass(slots=True)
class SessionState:
    session_id: str
    lifecycle: Pausable
    tool_active: bool = False
    eviction_eligible: bool = False
    last_used: float = 0.0


class AdmissionTimeout(RuntimeError):
    pass


class PolicyEventExecutor:
    """Small per-arm executor for nonblocking model lifecycle events."""

    def __init__(self, *, workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="policy-event"
        )

    def submit(self, operation: Callable[..., object], *args: object, **kwargs: object) -> None:
        # ThreadPoolExecutor.submit only enqueues work; callers are never
        # allowed to perform checkpoint/restore work in the gateway callback.
        self._executor.submit(operation, *args, **kwargs)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)


class PolicyCoordinator:
    """One process-wide admission ledger and LRU view for an experiment arm."""

    def __init__(self, policy: PolicySpec, *, budget_mib: int,
                 emergency_free_mib: int, operation_headroom_mib: int,
                 physical_sample: Callable[[], tuple[int, int]] | None = None) -> None:
        self.policy = policy
        self.budget_bytes = budget_mib * MIB
        self.emergency_free_bytes = emergency_free_mib * MIB
        self.operation_headroom_bytes = operation_headroom_mib * MIB
        self.operation_headroom_mib = operation_headroom_mib
        self.physical_sample = physical_sample or (lambda: (0, 1 << 62))
        self._condition = Condition()
        self._reservations: dict[str, int] = {}
        self._waiters: deque[object] = deque()
        self._sessions: dict[str, SessionState] = {}
        self.blocked_seconds = 0.0
        self.peak_commitment_bytes = 0
        self.pause_count = 0
        self.pause_service_seconds = 0.0
        self.resume_count = 0
        self.resume_service_seconds = 0.0
        self.admission_count = 0
        self.max_admission_queue_depth = 0
        self._admission_wait_samples: list[float] = []

    def register(self, session_id: str, lifecycle: Pausable) -> None:
        with self._condition:
            self._sessions[session_id] = SessionState(session_id, lifecycle, last_used=time.monotonic())

    def unregister(self, session_id: str) -> None:
        with self._condition:
            self._sessions.pop(session_id, None)
            self._reservations.pop(session_id, None)
            self._condition.notify_all()

    def set_tool_active(self, session_id: str, active: bool) -> None:
        with self._condition:
            state = self._sessions[session_id]
            state.tool_active = active
            state.eviction_eligible = not active
            state.last_used = time.monotonic()
            self._condition.notify_all()

    def tool_active(self, session_id: str) -> bool:
        with self._condition:
            return self._sessions[session_id].tool_active

    def set_eviction_eligible(self, session_id: str, eligible: bool) -> None:
        with self._condition:
            state = self._sessions[session_id]
            state.eviction_eligible = eligible
            state.last_used = time.monotonic()

    def pressure(self, additional_bytes: int = 0) -> bool:
        used, available = self.physical_sample()
        committed = sum(self._reservations.values()) + additional_bytes
        charged = max(used, committed) + self.operation_headroom_bytes
        return charged > self.budget_bytes or available < self.emergency_free_bytes

    def acquire(self, session_id: str, amount_mib: int, timeout_s: float) -> float:
        started = time.monotonic()
        amount = amount_mib * MIB
        if amount == 0:
            elapsed = time.monotonic() - started
            with self._condition:
                self.admission_count += 1
                self._admission_wait_samples.append(elapsed)
            return elapsed
        deadline = started + timeout_s
        ticket = object()
        with self._condition:
            self._waiters.append(ticket)
            self.max_admission_queue_depth = max(
                self.max_admission_queue_depth, len(self._waiters),
            )
            try:
                while self._waiters[0] is not ticket or self.pressure(amount):
                    victim = self._select_victim_locked(exclude=session_id)
                    if self._waiters[0] is ticket and victim is not None:
                        self._condition.release()
                        try:
                            elapsed = victim.lifecycle.checkpoint_and_evict()
                        finally:
                            self._condition.acquire()
                        victim.eviction_eligible = False
                        self.pause_count += 1
                        self.pause_service_seconds += elapsed
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AdmissionTimeout(f"memory admission timed out for {session_id}")
                    self._condition.wait(min(0.2, remaining))
                self._waiters.popleft()
                self._reservations[session_id] = self._reservations.get(session_id, 0) + amount
                self.peak_commitment_bytes = max(
                    self.peak_commitment_bytes, sum(self._reservations.values()))
                self._condition.notify_all()
            except Exception:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                    self._condition.notify_all()
                waited = time.monotonic() - started
                self.blocked_seconds += waited
                self.admission_count += 1
                self._admission_wait_samples.append(waited)
                raise
        waited = time.monotonic() - started
        with self._condition:
            self.blocked_seconds += waited
            self.admission_count += 1
            self._admission_wait_samples.append(waited)
        return waited

    def admission_metrics(self) -> dict[str, float | int | None]:
        """Return immutable aggregate metrics for the FIFO admission ledger."""
        with self._condition:
            samples = sorted(self._admission_wait_samples)
            max_queue_depth = self.max_admission_queue_depth

        def quantile(q: float) -> float | None:
            if not samples:
                return None
            position = (len(samples) - 1) * q
            lower = int(position)
            upper = min(lower + 1, len(samples) - 1)
            return samples[lower] + (samples[upper] - samples[lower]) * (
                position - lower
            )

        count = len(samples)
        total = sum(samples)
        return {
            "discipline": "fifo",
            "admission_count": count,
            "max_queue_depth": max_queue_depth,
            "wait_total_seconds": total,
            "wait_mean_seconds": total / count if count else None,
            "wait_p50_seconds": quantile(0.50),
            "wait_p95_seconds": quantile(0.95),
            "wait_max_seconds": max(samples) if samples else None,
        }

    def release(self, session_id: str, amount_mib: int) -> None:
        amount = amount_mib * MIB
        with self._condition:
            current = self._reservations.get(session_id, 0)
            if amount > current:
                raise RuntimeError(f"reservation underflow for {session_id}")
            if amount == current:
                self._reservations.pop(session_id, None)
            else:
                self._reservations[session_id] = current - amount
            self._condition.notify_all()

    def victim_for_restore(self, session_id: str) -> SessionState | None:
        with self._condition:
            return self._select_victim_locked(exclude=session_id)

    def model_wait_plan(self, duration_s: float) -> tuple[float | None, float | None]:
        """Return (pause delay, proactive restore lead); None means no operation."""
        if self.policy.reclamation is ReclamationPolicy.RESIDENT:
            return None, None
        if self.policy.eviction is EvictionPolicy.EAGER:
            delay = 0.0
        elif self.policy.eviction is EvictionPolicy.FIXED_DELAY:
            delay = self.policy.fixed_delay_seconds or 0.0
            if duration_s <= delay:
                return None, None
        elif self.policy.eviction is EvictionPolicy.WAIT_AWARE_PRESSURE:
            if duration_s <= 0 or not self.pressure():
                return None, None
            delay = 0.0
        else:
            return None, None
        lead = (self.policy.prefetch_lead_seconds or 0.0) \
            if self.policy.restore is RestorePolicy.PROACTIVE else None
        return delay, lead

    def restore(self, session_id: str, operation: Callable[[], float], timeout_s: float) -> float:
        """Admit restore and make exactly one capacity-recovery attempt."""
        self.acquire(session_id, self.operation_headroom_mib, timeout_s)
        try:
            try:
                elapsed = operation()
                self.resume_count += 1
                self.resume_service_seconds += elapsed
                return elapsed
            except Exception as first:
                if getattr(first, "status_code", None) != 409:
                    raise
                victim = self.victim_for_restore(session_id)
                if victim is None:
                    raise RuntimeError(
                        "CubeSandbox restore capacity rejection with no eligible victim"
                    ) from first
                elapsed = victim.lifecycle.checkpoint_and_evict()
                with self._condition:
                    victim.eviction_eligible = False
                    self.pause_count += 1
                    self.pause_service_seconds += elapsed
                elapsed = operation()  # second rejection is deliberately final
                self.resume_count += 1
                self.resume_service_seconds += elapsed
                return elapsed
        finally:
            self.release(session_id, self.operation_headroom_mib)

    def _select_victim_locked(self, *, exclude: str) -> SessionState | None:
        if self.policy.reclamation is ReclamationPolicy.RESIDENT:
            return None
        candidates = [state for state in self._sessions.values()
                      if state.session_id != exclude and state.eviction_eligible
                      and not state.tool_active and state.lifecycle.resident]
        return min(candidates, key=lambda state: state.last_used, default=None)
