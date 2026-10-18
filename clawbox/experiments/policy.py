from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from threading import Condition, Thread
from typing import Protocol

from .spec import EvictionPolicy, PolicySpec, ReclamationPolicy, RestorePolicy
from .spec_types import SnapshotTier
from .admission import DemandLedger
from .snapshot_pool import WarmCapacityError

MIB = 1024 * 1024


class Pausable(Protocol):
    @property
    def resident(self) -> bool: ...
    def checkpoint_and_evict(self, *, tier: SnapshotTier | None = None) -> float | None: ...


@dataclass(slots=True)
class SessionState:
    session_id: str
    lifecycle: Pausable
    tool_active: bool = False
    eviction_eligible: bool = False
    eviction_in_progress: bool = False
    last_used: float = 0.0
    runtime_lifecycle: Pausable | None = None
    runtime_before_pause: Callable[[], None] | None = None
    wait_id: str | None = None
    wait_started_monotonic_s: float | None = None
    response_ready: bool = False


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    session_id: str
    amount_bytes: int
    wait_class: str
    capacity_claim: bool


class AdmissionTimeout(RuntimeError):
    pass


@contextmanager
def checkpoint_reservations(lifecycles, tier):
    """Admit the whole pair before pause; preserve configured tiered fallback."""
    lifecycles = [vm for vm in lifecycles if vm is not None]
    with ExitStack() as reservations:
        try:
            for vm in lifecycles:
                reserve = getattr(vm, "reserve_checkpoint", None)
                if reserve is not None:
                    reservations.enter_context(reserve(tier=tier))
        except WarmCapacityError:
            reservations.close()
            if tier is not None or not all(
                    getattr(vm, "snapshot_storage", None) == "tiered"
                    and getattr(vm, "cold_snapshot_root", None) for vm in lifecycles):
                raise
            tier = SnapshotTier.COLD
            for vm in lifecycles:
                reservations.enter_context(vm.reserve_checkpoint(tier=tier))
        yield tier


class PolicyEventExecutor:
    """Small per-arm executor for nonblocking model lifecycle events."""

    def __init__(self, *, workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="policy-event"
        )

    def submit(self, operation: Callable[..., object], *args: object, **kwargs: object):
        # ThreadPoolExecutor.submit only enqueues work; callers are never
        # allowed to perform checkpoint/restore work in the gateway callback.
        return self._executor.submit(operation, *args, **kwargs)

    def close(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)


class PolicyCoordinator:
    """Admission ledger and LRU view for one compute memory domain in an arm."""

    def __init__(self, policy: PolicySpec, *, budget_mib: int,
                 emergency_free_mib: int, operation_headroom_mib: int,
                 progress_budget_mib: int | None = None,
                 startup_headroom_mib: int = 0,
                 os_managed_lifecycle: bool = False,
                 demand_ledger: DemandLedger | None = None,
                 demand_scope: str = "local",
                 reclaim_cache: Callable[[], None] | None = None,
                 physical_sample: Callable[[], tuple[int, int]] | None = None,
                 on_pressure_pause: Callable[
                     [SessionState, float | None, float | None, str], None
                 ] | None = None,
                 ) -> None:
        self.policy = policy
        self.os_managed_lifecycle = os_managed_lifecycle
        self.budget_bytes = budget_mib * MIB
        self.progress_budget_bytes = (
            progress_budget_mib * MIB
            if progress_budget_mib is not None else self.budget_bytes
        )
        if self.progress_budget_bytes < self.budget_bytes:
            raise ValueError("progress budget cannot be below the admission budget")
        self.emergency_free_bytes = emergency_free_mib * MIB
        self.operation_headroom_bytes = operation_headroom_mib * MIB
        self.operation_headroom_mib = operation_headroom_mib
        self.startup_headroom_bytes = startup_headroom_mib * MIB
        self.reclaim_cache = reclaim_cache
        self._last_cache_reclaim = float("-inf")
        self._cache_reclaim_running = False
        self._cache_reclaim_error: Exception | None = None
        self.physical_sample = physical_sample or (lambda: (0, 1 << 62))
        self.on_pressure_pause = on_pressure_pause
        self.pressure_relief: Callable[[], bool] | None = None
        self.checkpoint_capacity_skips = 0
        self.demand_scope = demand_scope
        self.demand = demand_ledger or DemandLedger(
            sample=self.physical_sample, capacity=self.progress_budget_bytes,
            free_floor=self.emergency_free_bytes)
        self._condition = self.demand.condition
        self._memory_samples = {}

        # Incremental reservations cover memory not represented by the current
        # host sample yet (VM create/restore) or memory a Tool is predicted to
        # add. Lifetime capacity claims describe an already-resident footprint
        # and therefore must not be added to that same observed footprint.
        self._reservations: dict[str, int] = {}
        self._capacity_claims: dict[str, int] = {}
        self._waiters: deque[object] = deque()
        self._waiter_requests: dict[object, AdmissionRequest] = {}
        self._progress_waiters: set[object] = set()
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
        self._lifecycle_wait_samples: dict[str, list[float]] = {
            "create": [], "restore": [],
        }
        self.safety_intervention_count = 0
        self.safety_interventions_by_reason: dict[str, int] = {}
        self._new_session_admission_blocked = False

    def set_new_session_admission_blocked(self, blocked: bool) -> None:
        """Latch only new VM-pair admission; existing sessions must make progress."""
        with self._condition:
            self._new_session_admission_blocked = blocked
            self._condition.notify_all()

    def register(self, session_id: str, lifecycle: Pausable) -> None:
        with self._condition:
            self._sessions[session_id] = SessionState(session_id, lifecycle, last_used=time.monotonic())

    def register_runtime(
        self, session_id: str, lifecycle: Pausable, *,
        before_pause: Callable[[], None] | None = None,
    ) -> None:
        with self._condition:
            self._sessions[session_id].runtime_lifecycle = lifecycle
            self._sessions[session_id].runtime_before_pause = before_pause

    def begin_model_wait(self, session_id: str, wait_id: str) -> None:
        now = time.monotonic()
        with self._condition:
            state = self._sessions[session_id]
            state.wait_id = wait_id
            state.wait_started_monotonic_s = now
            state.response_ready = False
            state.eviction_eligible = True
            state.last_used = now
            self._condition.notify_all()

    def complete_model_wait(self, session_id: str, wait_id: str) -> None:
        with self._condition:
            state = self._sessions[session_id]
            if state.wait_id != wait_id:
                return
            state.response_ready = True
            state.eviction_eligible = False
            # Admission may already have atomically claimed this waiting pair.
            # Let that checkpoint finish before the gateway publishes its
            # response; the response callback can then restore Runtime first.
            while state.eviction_in_progress:
                self._condition.wait()
            self._condition.notify_all()

    def unregister(self, session_id: str) -> None:
        with self._condition:
            self._sessions.pop(session_id, None)
            self._memory_samples.pop(session_id, None)
            for key in list(self.demand.claims):
                if key[0] == session_id:
                    self.demand.release(key)
            self._reservations.pop(session_id, None)
            self._capacity_claims.pop(session_id, None)
            self._condition.notify_all()

    def set_tool_active(self, session_id: str, active: bool) -> None:
        with self._condition:
            state = self._sessions[session_id]
            # Victim selection claims a session before dropping the coordinator
            # lock for the blocking Cube checkpoint.  A new Tool operation (or
            # final validation) must wait for that already-committed eviction,
            # then restore through the normal managed path.
            while active and state.eviction_in_progress:
                self._condition.wait()
            state.tool_active = active
            state.eviction_eligible = not active
            state.last_used = time.monotonic()
            self._condition.notify_all()

    def set_tool_active_if_registered(self, session_id: str, active: bool) -> bool:
        """Update a late cleanup callback without reviving a closed session."""
        with self._condition:
            state = self._sessions.get(session_id)
            if state is None:
                return False
            while active and state.eviction_in_progress:
                self._condition.wait()
            state.tool_active = active
            state.eviction_eligible = not active
            state.last_used = time.monotonic()
            self._condition.notify_all()
            return True

    def claim_memory_return(self, session_id: str) -> bool:
        """Atomically keep Tool admission and an idle NUMA return apart."""
        with self._condition:
            state = self._sessions.get(session_id)
            if (
                state is None
                or state.tool_active
                or state.eviction_in_progress
                or (session_id, "tool_admission") in self.demand.claims
            ):
                return False
            state.eviction_in_progress = True
            return True

    def release_memory_return(self, session_id: str) -> None:
        with self._condition:
            state = self._sessions.get(session_id)
            if state is not None:
                state.eviction_in_progress = False
            self._condition.notify_all()

    def begin_tool_admission(self, session_id: str, amount_mib: int,
                             timeout_s: float, *, total_mib: int | None = None) -> float:
        """Reserve Tool growth after the caller has chosen its allocation tier.

        The caller marks the session active before restore and routing.  Keep
        that protection while ``acquire`` waits so another admission cannot
        select this resident Tool as a victim.
        """
        self.set_tool_active(session_id, True)
        try:
            return self.acquire(session_id, amount_mib, timeout_s, total_mib=total_mib)
        except Exception:
            self.set_tool_active(session_id, False)
            raise

    def tool_active(self, session_id: str) -> bool:
        with self._condition:
            return self._sessions[session_id].tool_active

    def set_eviction_eligible(self, session_id: str, eligible: bool) -> None:
        with self._condition:
            state = self._sessions[session_id]
            state.eviction_eligible = eligible
            state.last_used = time.monotonic()

    def register_memory(self, session_id, *, tool, lifecycle):
        """Readers return total and home-node charges for stable VM leaves."""
        with self._condition:
            self._memory_samples[session_id] = {'tool_admission': tool,
                                               'create': lifecycle, 'restore': lifecycle}
            # Placement may reserve the pair before the session registers its
            # empty, stable leaves. Bind that existing claim without reserving
            # again; observed creation growth then reduces pending demand.
            claim = self.demand.claims.get((session_id, 'create'))
            if claim is not None:
                claim.sample = lifecycle
                claim.baseline = lifecycle()

    def bind_tool_memory(self, session_id):
        key = (session_id, 'tool_admission')
        with self._condition:
            claim = self.demand.claims.get(key)
            sample = self._memory_samples.get(session_id, {}).get('tool_admission')
        if claim is None or sample is None:
            return
        # Kernel NUMA accounting can block. Keep the nominal reservation
        # charged while reading, so other sessions can still make progress.
        baseline = sample()
        with self._condition:
            if self.demand.claims.get(key) is claim:
                claim.baseline = baseline
                claim.sample = sample

    def pressure(self, additional_bytes: int = 0) -> bool:
        with self._condition:
            return bool(self._pressure_reasons(additional_bytes))

    def projected_local_commitment(self, additional_mib: int = 0) -> int:
        """Return LOCAL usage plus still-unmaterialized LOCAL reservations."""
        with self._condition:
            used, _available = self.physical_sample()
            return (
                max(used, sum(self._capacity_claims.values()))
                + self.demand.pending(self.demand_scope)
                + additional_mib * MIB
                + self.operation_headroom_bytes
            )

    def local_commitment_exceeds(self, additional_mib: int, limit_bytes: int) -> bool:
        """Avoid per-claim reads when even the nominal upper bound fits."""
        with self._condition:
            used, _available = self.physical_sample()
            upper = (max(used, sum(self._capacity_claims.values()))
                     + self.demand.pending_upper_bound(self.demand_scope)
                     + additional_mib*MIB + self.operation_headroom_bytes)
            if upper <= limit_bytes:
                self.demand.pending_upper_bound_fast_path_count += 1
                return False
            return self.projected_local_commitment(additional_mib) > limit_bytes

    def blocked_demand_pressure(self) -> bool:
        """Include the next runnable blocked admission in pressure detection."""
        with self._condition:
            head = self._admission_head_locked()
            if head is None:
                return self.pressure()
            request = self._waiter_requests[head]
            amount = request.amount_bytes
            if (request.wait_class == "create"
                    and self._startup_headroom_needed(request.session_id)):
                amount += self.startup_headroom_bytes
            return bool(self._pressure_reasons(
                amount, capacity_claim=request.capacity_claim,
                existing_progress=(request.wait_class == "tool_admission"),
            ))

    def resource_feedback(self) -> dict:
        """Read the existing LOCAL admission ledger; never add a second queue."""
        with self._condition:
            if not self._waiters:
                return {'memory_blocked': False, 'scope': self.demand_scope, 'waiters': 0}
            used, available = self.physical_sample()
            head = self._admission_head_locked()
            if head is None:
                return {'memory_blocked': False, 'scope': self.demand_scope,
                        'waiters': len(self._waiters), 'reasons': ['execution_already_inflight'],
                        'local_used_bytes': used, 'local_available_bytes': available,
                        'deficit_bytes': 0}
            request = self._waiter_requests[head]
            reasons = self._pressure_reasons(request.amount_bytes,
                capacity_claim=request.capacity_claim,
                existing_progress=request.wait_class == 'tool_admission')
            memory_reasons = set(reasons) - {'execution_already_inflight'}
            extra = request.amount_bytes+self.operation_headroom_bytes
            upper_deficit = used+self.demand.pending_upper_bound(self.demand_scope)+extra-self.budget_bytes
            deficit = (0 if upper_deficit<=0 else
                       max(0,used+self.demand.pending(self.demand_scope)+extra-self.budget_bytes))
            return {'memory_blocked': bool(memory_reasons), 'scope': self.demand_scope,
                    'waiters': len(self._waiters), 'reasons': list(reasons),
                    'local_used_bytes': used, 'local_available_bytes': available,
                    'deficit_bytes': deficit}

    def _pressure_reasons(self, additional_bytes: int = 0, *,
                          capacity_claim: bool = False,
                          existing_progress: bool = False,
                          total_bytes: int | None = None) -> tuple[str, ...]:
        used, available = self.physical_sample()
        capacity = sum(self._capacity_claims.values())
        if capacity_claim:
            capacity += additional_bytes
        effective_budget = (
            self.progress_budget_bytes
            if existing_progress and (
                used >= self.budget_bytes or not self._reservations
            )
            else self.budget_bytes
        )
        global_used, global_available = self.demand.sample()
        global_additional = additional_bytes if total_bytes is None else total_bytes
        local_upper = self.demand.pending_upper_bound(self.demand_scope)
        global_upper = self.demand.pending_upper_bound()
        local_charge_upper = (
            max(used, capacity)
            + local_upper
            + (0 if capacity_claim else additional_bytes)
            + self.operation_headroom_bytes
        )
        global_charge_upper = (
            global_used + global_upper + self.demand.margin
            + global_additional + self.operation_headroom_bytes
        )
        # The nominal reservations are an upper bound on unmaterialized demand.
        # If that conservative bound fits both the local policy budget and the
        # shared physical budget, per-claim cgroup reads cannot change the
        # decision. Avoid serializing O(active claims) filesystem reads under
        # the global admission lock in this common, low-pressure case.
        if (local_charge_upper <= effective_budget
                and global_charge_upper <= self.demand.capacity):
            incremental = local_upper
            global_pending = global_upper
            self.demand.pending_upper_bound_fast_path_count += 1
        else:
            incremental = self.demand.pending(self.demand_scope)
            global_pending = self.demand.pending()
        if not capacity_claim:
            incremental += additional_bytes
        charged = max(used, capacity) + incremental + self.operation_headroom_bytes
        reasons = []
        if charged > effective_budget and not (existing_progress and additional_bytes == 0):
            reasons.append("configured_memory_budget")
        if available < self.emergency_free_bytes:
            reasons.append("emergency_free_memory")
        reasons.extend(self.demand.reasons(
            additional_bytes if total_bytes is None else total_bytes,
            self.operation_headroom_bytes, pending=global_pending,
            sampled=(global_used, global_available)))
        return tuple(dict.fromkeys(reasons))

    def _startup_headroom_needed(self, session_id: str) -> bool:
        """A first VM pair cannot starve work because no older pair can run."""
        for other_id, state in self._sessions.items():
            if other_id == session_id or not state.lifecycle.resident:
                continue
            if state.runtime_lifecycle is None or state.runtime_lifecycle.resident:
                return True
        return False

    def _admission_head_locked(self):
        """Keep FIFO among runnable requests, prioritizing existing progress."""
        first = None
        for ticket in self._waiters:
            request = self._waiter_requests[ticket]
            if (request.session_id, request.wait_class) in self.demand.claims:
                continue  # Same-VM polling must not stall unrelated VMs.
            if first is None:
                first = ticket
            if ticket in self._progress_waiters:
                return ticket
        return first

    def acquire(self, session_id: str, amount_mib: int, timeout_s: float | None, *,
                wait_class: str = "tool_admission",
                capacity_claim: bool = False, total_mib: int | None = None) -> float:
        started = time.monotonic()
        amount = amount_mib * MIB
        total = (amount_mib if total_mib is None else total_mib) * MIB
        if self.os_managed_lifecycle and wait_class == "create":
            # No configured-capacity or measured-budget admission in the OS
            # deployment. The explicit host emergency floor remains a guard.
            if amount or total:
                raise ValueError("OS-managed creation must not reserve configured memory")
            if self.physical_sample()[1] < self.emergency_free_bytes:
                raise AdmissionTimeout("host emergency free-memory floor reached; reduce concurrency or free host memory")
            return 0.0
        if amount == 0 and total == 0 and wait_class not in {"create", "restore"}:
            elapsed = time.monotonic() - started
            with self._condition:
                self._record_wait_locked(wait_class, elapsed)
            return elapsed
        deadline = None if timeout_s is None else started + timeout_s
        ticket = object()
        with self._condition:
            self._waiters.append(ticket)
            self._waiter_requests[ticket] = AdmissionRequest(
                session_id=session_id,
                amount_bytes=amount,
                wait_class=wait_class,
                capacity_claim=capacity_claim,
            )
            if wait_class != "create" and not capacity_claim:
                self._progress_waiters.add(ticket)
            recorded_safety_reasons: set[str] = set()
            if wait_class == "tool_admission":
                self.max_admission_queue_depth = max(
                    self.max_admission_queue_depth, len(self._waiters),
                )
            try:
                while True:
                    if self._cache_reclaim_error is not None:
                        raise RuntimeError("LOCAL cache reclamation failed") from self._cache_reclaim_error
                    # Existing sessions must be able to finish and free memory
                    # while a new VM pair is waiting for capacity.
                    head = self._admission_head_locked()
                    at_head = head is ticket
                    safety_reasons = self._pressure_reasons(
                        amount + (self.startup_headroom_bytes
                                  if wait_class == "create"
                                  and self._startup_headroom_needed(session_id) else 0),
                        capacity_claim=capacity_claim,
                        existing_progress=(wait_class == "tool_admission"),
                        total_bytes=total + (self.startup_headroom_bytes if wait_class == "create"
                                            and self._startup_headroom_needed(session_id) else 0),
                    ) if at_head else ()
                    if (session_id, wait_class) in self.demand.claims:
                        safety_reasons = (*safety_reasons, 'execution_already_inflight')
                    if (
                        at_head and wait_class == "create"
                        and self._new_session_admission_blocked
                    ):
                        safety_reasons = (*safety_reasons, "local_high_hysteresis")
                    if at_head and not safety_reasons:
                        break
                    # Admission stops below memory.max, so the kernel may never
                    # experience pressure that would reclaim clean file cache.
                    # Request real, cgroup-scoped reclamation before evicting VMs.
                    if (at_head and "configured_memory_budget" in safety_reasons
                            and self.reclaim_cache is not None
                            and not self._cache_reclaim_running
                            and time.monotonic() - self._last_cache_reclaim >= 2.0):
                        self._cache_reclaim_running = True
                        # memory.reclaim can remain in the kernel long after
                        # enough pages were freed. Never pin the admission head
                        # to syscall completion; actual usage is sampled below.
                        Thread(target=self._reclaim_cache, name="local-cache-reclaim",
                               daemon=True).start()
                    for reason in safety_reasons:
                        if reason not in recorded_safety_reasons:
                            recorded_safety_reasons.add(reason)
                            self.safety_intervention_count += 1
                            self.safety_interventions_by_reason[reason] = (
                                self.safety_interventions_by_reason.get(reason, 0) + 1
                            )
                    if at_head and 'execution_already_inflight' not in safety_reasons:
                        self._condition.release()
                        try:
                            relieved = self.relieve_pressure(exclude=session_id)
                        finally:
                            self._condition.acquire()
                        if relieved:
                            continue
                    remaining = 0.2 if deadline is None else deadline - time.monotonic()
                    if remaining <= 0:
                        raise AdmissionTimeout(f"memory admission timed out for {session_id}")
                    self._condition.wait(min(0.2, remaining))
                self._waiters.remove(ticket)
                self._waiter_requests.pop(ticket, None)
                self._progress_waiters.discard(ticket)
                self.demand.add((session_id, wait_class), scope=self.demand_scope,
                    amount=total, local_amount=amount,
                    sample=(self._memory_samples.get(session_id, {}).get(wait_class)
                            if wait_class != 'tool_admission' else None))
                ledger = self._capacity_claims if capacity_claim else self._reservations
                ledger[session_id] = ledger.get(session_id, 0) + amount
                self.peak_commitment_bytes = max(
                    self.peak_commitment_bytes,
                    sum(self._capacity_claims.values()) + sum(self._reservations.values()),
                )
                self._condition.notify_all()
            except Exception:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                self._waiter_requests.pop(ticket, None)
                self._progress_waiters.discard(ticket)
                self._condition.notify_all()
                waited = time.monotonic() - started
                self._record_wait_locked(wait_class, waited)
                raise
        waited = time.monotonic() - started
        with self._condition:
            self._record_wait_locked(wait_class, waited)
        return waited

    def _reclaim_cache(self) -> None:
        try:
            self.reclaim_cache()
        except Exception as exc:
            with self._condition:
                self._cache_reclaim_error = exc
        finally:
            with self._condition:
                self._cache_reclaim_running = False
                self._last_cache_reclaim = time.monotonic()
                self._condition.notify_all()

    def _record_wait_locked(self, wait_class: str, waited: float) -> None:
        if wait_class == "tool_admission":
            self.blocked_seconds += waited
            self.admission_count += 1
            self._admission_wait_samples.append(waited)
            return
        if wait_class not in self._lifecycle_wait_samples:
            raise ValueError(f"unknown reservation wait class {wait_class!r}")
        self._lifecycle_wait_samples[wait_class].append(waited)

    def admission_metrics(self) -> dict[str, float | int | None]:
        """Return immutable aggregate metrics for the FIFO admission ledger."""
        with self._condition:
            samples = sorted(self._admission_wait_samples)
            max_queue_depth = self.max_admission_queue_depth
            lifecycle_samples = {
                key: tuple(values) for key, values in self._lifecycle_wait_samples.items()
            }

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
            "discipline": "runnable_progress_before_create_fifo",
            "admission_count": count,
            "max_queue_depth": max_queue_depth,
            "wait_total_seconds": total,
            "wait_mean_seconds": total / count if count else None,
            "wait_p50_seconds": quantile(0.50),
            "wait_p95_seconds": quantile(0.95),
            "wait_max_seconds": max(samples) if samples else None,
            "safety_intervention_count": self.safety_intervention_count,
            "safety_interventions_by_reason": dict(
                self.safety_interventions_by_reason
            ),
            "demand_pending_upper_bound_fast_path_count": (
                self.demand.pending_upper_bound_fast_path_count
            ),
            "demand_pending_exact_count": self.demand.pending_exact_count,
            "lifecycle_create_reservation_wait_seconds": sum(
                lifecycle_samples["create"]
            ),
            "lifecycle_restore_reservation_wait_seconds": sum(
                lifecycle_samples["restore"]
            ),
        }

    def release(self, session_id: str, amount_mib: int, *, wait_class='tool_admission') -> None:
        with self._condition:
            self._release_from(self._reservations, session_id, amount_mib)
            self.demand.release((session_id, wait_class))

    def acquire_capacity(self, session_id: str, amount_mib: int,
                         timeout_s: float) -> float:
        return self.acquire(
            session_id, amount_mib, timeout_s,
            wait_class="tool_admission", capacity_claim=True,
        )

    def release_capacity(self, session_id: str, amount_mib: int) -> None:
        self._release_from(self._capacity_claims, session_id, amount_mib)

    def _release_from(self, ledger: dict[str, int], session_id: str,
                      amount_mib: int) -> None:
        amount = amount_mib * MIB
        with self._condition:
            current = ledger.get(session_id, 0)
            if amount > current:
                raise RuntimeError(f"reservation underflow for {session_id}")
            if amount == current:
                ledger.pop(session_id, None)
            else:
                ledger[session_id] = current - amount
            self._condition.notify_all()

    def victim_for_restore(self, session_id: str) -> SessionState | None:
        with self._condition:
            return self._claim_victim_locked(exclude=session_id)

    def release_victim(self, victim: SessionState) -> None:
        with self._condition:
            victim.eviction_in_progress = False
            victim.eviction_eligible = False
            self._condition.notify_all()

    def watermark_reclaim_candidate(self) -> dict[str, object] | None:
        """Describe the current safe victim without claiming or pausing it.

        The controller uses this only for a cost estimate.  The subsequent
        atomic claim revalidates eligibility, so a concurrent Tool admission
        can still remove the candidate safely.
        """
        with self._condition:
            victim = self._select_victim_locked(exclude="")
            if victim is None:
                return None
            lifecycles = [victim.lifecycle]
            if (
                self.policy.runtime_snapshot
                and victim.runtime_lifecycle is not None
                and victim.runtime_lifecycle.resident
                and victim.wait_id is not None
                and not victim.response_ready
            ):
                lifecycles.append(victim.runtime_lifecycle)
            configured = sum(
                int(getattr(lifecycle, "configured_memory_bytes", 0) or 0)
                for lifecycle in lifecycles
            )
            reservation = sum(
                int(getattr(lifecycle, "snapshot_reservation_bytes", 0) or 0)
                for lifecycle in lifecycles
            )
            return {
                "session_id": victim.session_id,
                "configured_bytes": configured,
                "snapshot_reservation_bytes": reservation,
                "vm_count": len(lifecycles),
            }

    def relieve_pressure(self, *, exclude: str = "") -> bool:
        """Use the arm's common action selector for admission and idle callbacks."""
        if self.pressure_relief is not None:
            return self.pressure_relief()
        return self.evict_one_for_watermark(exclude=exclude, reason="memory_admission")

    def evict_one_for_watermark(self, *, tier: SnapshotTier | None = None,
                                exclude: str = "", reason: str = "local_high_watermark") -> bool:
        """Atomically claim one actual idle session, then checkpoint its configured scope."""
        with self._condition:
            victim = self._claim_victim_locked(exclude=exclude)
            if victim is None:
                return False
            # Once claimed, response delivery and Tool start wait for the whole
            # pair. Do not change scope halfway through when a response arrives.
            runtime = (victim.runtime_lifecycle if self.policy.runtime_snapshot
                       and victim.wait_id is not None and not victim.response_ready
                       and victim.runtime_lifecycle is not None
                       and victim.runtime_lifecycle.resident else None)
        capacity_skipped = False
        reservations_ready = False
        try:
            with checkpoint_reservations((victim.lifecycle, runtime), tier) as tier:
                reservations_ready = True
                tool_elapsed = (victim.lifecycle.checkpoint_and_evict()
                                if tier is None else victim.lifecycle.checkpoint_and_evict(tier=tier))
                runtime_elapsed = None
                if runtime is not None:
                    if victim.runtime_before_pause is not None:
                        victim.runtime_before_pause()
                    runtime_elapsed = (runtime.checkpoint_and_evict() if tier is None
                                       else runtime.checkpoint_and_evict(tier=tier))
            paused = sum(v is not None for v in (tool_elapsed, runtime_elapsed))
            with self._condition:
                self.pause_count += paused
                self.pause_service_seconds += sum(v for v in (tool_elapsed, runtime_elapsed) if v is not None)
            if paused and self.on_pressure_pause is not None:
                self.on_pressure_pause(victim, tool_elapsed, runtime_elapsed, reason)
            return bool(paused)
        except WarmCapacityError:
            if reservations_ready:
                # A post-pause accounting failure is not ordinary capacity
                # backpressure and must remain visible to the experiment.
                raise
            # Capacity exhaustion is ordinary backpressure, not an Agent or
            # arm failure. Pair reservations ensure neither VM paused for it.
            capacity_skipped = True
            self.checkpoint_capacity_skips += 1
            return False
        finally:
            with self._condition:
                self.release_victim(victim)
                if capacity_skipped:
                    victim.eviction_eligible = not victim.tool_active and not victim.response_ready

    def materialize(self, session_id: str, amount_mib: int,
                    operation: Callable[[], float], timeout_s: float) -> tuple[float, float]:
        """Reserve a VM's configured footprint before its create call."""
        waited = self.acquire(
            session_id, amount_mib, timeout_s, wait_class="create",
        )
        try:
            return operation(), waited
        finally:
            self.release(session_id, amount_mib, wait_class='create')

    def restore(self, session_id: str, amount_mib: int,
                operation: Callable[[], float], timeout_s: float) -> tuple[float, float]:
        """Reserve a VM footprint, restore, and try one capacity recovery."""
        waited = self.acquire(
            session_id, amount_mib, timeout_s, wait_class="restore",
        )
        try:
            try:
                elapsed = operation()
                self.resume_count += 1
                self.resume_service_seconds += elapsed
                return elapsed, waited
            except Exception as first:
                if getattr(first, "status_code", None) != 409:
                    raise
                if not self.evict_one_for_watermark(
                        exclude=session_id, reason="cube_restore_capacity"):
                    raise RuntimeError(
                        "CubeSandbox restore capacity rejection with no reclaimable idle VM pair; "
                        "free LOCAL/WARM capacity or reduce concurrency"
                    ) from first
                elapsed = operation()  # second rejection is deliberately final
                self.resume_count += 1
                self.resume_service_seconds += elapsed
                return elapsed, waited
        finally:
            self.release(session_id, amount_mib, wait_class='restore')

    def _select_victim_locked(self, *, exclude: str) -> SessionState | None:
        if self.policy.reclamation is ReclamationPolicy.RESIDENT:
            return None
        candidates = [state for state in self._sessions.values()
                      if state.session_id != exclude and state.eviction_eligible
                      and not state.eviction_in_progress and not state.tool_active
                      and state.lifecycle.resident]
        if self.policy.eviction is EvictionPolicy.WAIT_AWARE_PRESSURE:
            # Model duration is unknown online. A request remains idle until
            # its response is generated. No predicted or trace duration is consulted.
            candidates = [state for state in candidates
                          if state.wait_id is not None and not state.response_ready]
        return min(candidates, key=lambda state: state.last_used, default=None)

    def _claim_victim_locked(self, *, exclude: str) -> SessionState | None:
        victim = self._select_victim_locked(exclude=exclude)
        if victim is not None:
            victim.eviction_in_progress = True
        return victim
