from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import importlib.metadata
import json
import math
import os
import platform
import statistics
import subprocess
import base64
import shlex
import sys
import time
import traceback
import urllib.parse
from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event, Lock, RLock, Semaphore, Thread
from types import SimpleNamespace
from typing import Any, Callable

from clawbox.cube import (
    CubeCommandExecutor, CubeSandboxClient, CubeSandboxLifecycle,
    OwnedSandboxJournal, Ownership,
)
from .llm_config import resolve_llm_configuration
from clawbox.replay.trace import find_recordings, load_trace
from clawbox.replay.lifecycle import CommandResult, LifecycleError

from .memory import (
    cgroup_memory_evidence,
    CgroupMemorySampler, NodeMemorySampler, NumaCgroupMemorySampler,
    NumaNodeMemorySampler, SandboxRSSSampler,
)
from .clawtune_trace import ClawTuneTraceWriter
from .model_gateway import ManagedModelGateway, SessionGatewayState
from .native_artifacts import collect_and_validate_native_tool_artifacts
from .numa_borrow import SandboxNumaBorrower
from .memory_actions import MemoryActionCostModel
from .openclaw_driver import (
    NativeSSHConfig, NativeSSHRoute, native_ssh_host_key_alias, native_ssh_route,
    native_tool_bridge_setup_command, openclaw_real_ssh_path,
    OPENCLAW_SSH_RUNTIME_ROOT,
    native_tool_workspace_setup_command,
    run_openclaw,
)
from .policy import PolicyCoordinator, PolicyEventExecutor
from .congestion import locate_cgroup
from .pressure import PressureControl
from .priority import PriorityDomain, c2_applies
from .spec_types import RuntimeControl
from .snapshot_pool import WarmCapacityError, WarmSnapshotPool
from .topology import place_session
from .numa_placement import CpuMonitor, SessionPlacement
from .spec_types import InferenceBackend, SnapshotTier
from .policy_control import PolicyControlServer
from .prediction import P50PredictionProvider, PredictionUnavailable, prediction_provider_type
from .admission import DemandLedger
from .runtime_model_relay import RELAY_CHECKPOINT_URL
from .ssh_credentials import generate_ssh_credentials
from .results import FailureCategory, ResultEnvelope, RunStatus, failure_category_for, utcnow
from .spec import (
    AdmissionPolicy, AgentDriver, EvictionPolicy, ExperimentArm, ExperimentSpec,
    ReclamationPolicy, RestorePolicy, expand_matrix, load_experiment,
)


class CgroupCpuSampler:
    """Measure actual CPU consumed by the experiment parent cgroup."""

    def __init__(self, cgroup: Path) -> None:
        self.stat_path = cgroup / "cpu.stat"
        cpus = (cgroup / "cpuset.cpus.effective").read_text(encoding="utf-8").strip()
        self.capacity_cores = sum(
            (int(end) - int(start) + 1) if separator else 1
            for item in cpus.split(",") if item
            for start, separator, end in [item.partition("-")]
        )
        if self.capacity_cores < 1:
            raise RuntimeError(f"experiment cgroup has no effective CPUs: {cgroup}")
        self.samples: list[tuple[float, dict[str, int]]] = []
        self.sample()

    def _read(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for line in self.stat_path.read_text(encoding="utf-8").splitlines():
            name, value = line.split()
            values[name] = int(value)
        if "usage_usec" not in values:
            raise RuntimeError(f"cgroup CPU accounting is unavailable: {self.stat_path}")
        return values

    def sample(self) -> dict[str, int | float | None]:
        now = time.monotonic()
        values = self._read()
        average_cores = None
        if self.samples:
            previous_time, previous = self.samples[-1]
            elapsed_usec = (now - previous_time) * 1_000_000
            if elapsed_usec > 0:
                average_cores = max(
                    0.0, (values["usage_usec"] - previous["usage_usec"]) / elapsed_usec
                )
        self.samples.append((now, values))
        return {
            "capacity_cores": self.capacity_cores,
            "average_cores": average_cores,
            "utilization_fraction": (
                average_cores / self.capacity_cores
                if average_cores is not None else None
            ),
            **values,
        }

    def summary(self) -> dict[str, int | float | None]:
        self.sample()
        first_time, first = self.samples[0]
        last_time, last = self.samples[-1]
        elapsed = max(0.0, last_time - first_time)
        mean_cores = (
            max(0.0, last["usage_usec"] - first["usage_usec"]) / (elapsed * 1_000_000)
            if elapsed > 0 else None
        )
        interval_cores = [
            max(0.0, (current[1]["usage_usec"] - previous[1]["usage_usec"])
                / ((current[0] - previous[0]) * 1_000_000))
            for previous, current in zip(self.samples, self.samples[1:])
            if current[0] > previous[0]
        ]
        return {
            "measurement": "cgroup_v2_cpu_stat",
            "capacity_cores": self.capacity_cores,
            "elapsed_seconds": elapsed,
            "mean_cores": mean_cores,
            "peak_interval_cores": max(interval_cores) if interval_cores else None,
            "mean_utilization_fraction": (
                mean_cores / self.capacity_cores if mean_cores is not None else None
            ),
            "peak_interval_utilization_fraction": (
                max(interval_cores) / self.capacity_cores if interval_cores else None
            ),
            "usage_usec_delta": max(0, last["usage_usec"] - first["usage_usec"]),
            "user_usec_delta": max(0, last.get("user_usec", 0) - first.get("user_usec", 0)),
            "system_usec_delta": max(
                0, last.get("system_usec", 0) - first.get("system_usec", 0)
            ),
            "nr_throttled_delta": max(
                0, last.get("nr_throttled", 0) - first.get("nr_throttled", 0)
            ),
            "throttled_usec_delta": max(
                0, last.get("throttled_usec", 0) - first.get("throttled_usec", 0)
            ),
            "sample_count": len(self.samples),
        }


class ArmInfrastructureError(RuntimeError):
    """Shared controls or session cleanup can no longer preserve arm isolation."""


def collect_session_results(futures, *, timeout, sessions, failures, events,
                            arm_id, check_health):
    """Drain independent outcomes; only shared failures escape to arm cleanup."""
    first_failure = None
    for future in as_completed(futures, timeout=timeout):
        try:
            result = future.result()
        except Exception as exc:
            record = {
                "event": "session_failed",
                "session_id": f"{arm_id}-{futures[future]:04d}",
                "error": str(exc), "error_type": type(exc).__name__,
            }
            failures.append(record)
            events.write(record)
            if isinstance(exc, ArmInfrastructureError):
                raise
            check_health()
            first_failure = first_failure or exc
        else:
            sessions.append(result)
            check_health()
    return first_failure


def classify_arm_failure(failure, *, domain_oom_kills=0, infrastructure_error=None,
                         session_failure=False):
    """Preserve overload outcomes without hiding control or cleanup failures."""
    if infrastructure_error is not None:
        return FailureCategory.INFRASTRUCTURE
    if domain_oom_kills:
        return FailureCategory.OOM
    if isinstance(failure, TimeoutError):
        return FailureCategory.TIMEOUT
    if failure is None:
        return None
    category = failure_category_for(RunStatus.FAILED, str(failure))
    if session_failure and category is FailureCategory.INFRASTRUCTURE:
        return FailureCategory.AGENT
    return category


def drain_policy_futures(pending: list[Any], lock: Any, timeout_s: float | None) -> None:
    """Observe every submitted result, including already completed failures."""
    consumed = 0
    while True:
        with lock:
            batch = pending[consumed:]
        if not batch:
            return
        for future in batch:
            future.result(timeout=timeout_s)
            consumed += 1


def wait_for_admission_without_lifecycle_lock(
    lifecycle_lock: Any, operation: Any,
) -> Any:
    """Run a blocking memory admission while its completion path stays runnable.

    The caller owns ``lifecycle_lock`` through a surrounding context manager.
    Reacquiring in ``finally`` preserves that context even when admission fails.
    """
    lifecycle_lock.release()
    try:
        return operation()
    finally:
        lifecycle_lock.acquire()


def session_case_for(arm: ExperimentArm, session_index: int) -> Any:
    cases = arm.session_cases or (arm.case,)
    return cases[session_index % len(cases)]


@contextmanager
def creation_slot(gate, events, session_id):
    """Queue before reserving memory; release at pair readiness, not task end."""
    started = time.time()
    events.write({'event':'sandbox_create_gate_wait_started','session_id':session_id})
    gate.acquire()
    acquired = time.time()
    held = True
    def release():
        nonlocal held
        if held:
            held = False
            gate.release()
    try:
        events.write({'event':'sandbox_create_gate_acquired','session_id':session_id,
                      'wait_seconds':acquired-started})
        yield started, acquired, release
    finally:
        release()


def read_priority_memory_charge(path: Path, local_numa_node: int | None) -> tuple[int, int]:
    """Read one session's charge without failing if cleanup wins the race."""
    try:
        total = int((path / "memory.current").read_text())
        if local_numa_node is None:
            return total, total
        from .numa_borrow import read_numa_lru_bytes
        nodes, unknown = read_numa_lru_bytes(path)
        return total, nodes.get(local_numa_node, 0) + unknown
    except FileNotFoundError:
        # Cube may remove an emptied session cgroup immediately after its VMs
        # are destroyed but just before coordinator.unregister() drops the
        # corresponding demand callback.  Its remaining charge is then zero.
        # Preserve errors for a missing controller file on a live cgroup.
        if not path.exists():
            return 0, 0
        raise


def policy_operation_headroom_mib(arm: ExperimentArm) -> int:
    """Charge checkpoint/restore workspace only to policies that use it."""
    if arm.policy.reclamation is not ReclamationPolicy.SNAPSHOT_PAUSE:
        return 0
    return arm.resources.checkpoint_restore_headroom_mib


def policy_progress_budget_mib(arm: ExperimentArm) -> int | None:
    """LOCAL growth may use its hard watermark, never unfunded shared capacity."""
    return arm.resources.local_memory_capacity_mib


def policy_event_workers(arm: ExperimentArm) -> int:
    """Apply the documented snapshot-transfer limit to pause/restore RPCs."""
    return arm.resources.pool_transfer_concurrency


class WatermarkController:
    """Enforce LOW/HIGH LOCAL hysteresis with shared-memory OOM protection."""

    def __init__(self, arm: ExperimentArm, coordinator: PolicyCoordinator,
                 sampler: NumaCgroupMemorySampler, borrower: SandboxNumaBorrower | None,
                 events: "EventWriter", *, shared_pool: WarmSnapshotPool | None = None,
                 node_id: str | None = None) -> None:
        self.arm = arm
        self.coordinator = coordinator
        self.sampler = sampler
        self.borrower = borrower
        self.shared_pool = shared_pool if shared_pool is not None else borrower.shared_pool
        self.node_id = node_id if node_id is not None else (borrower.node_id if borrower else None)
        self.events = events
        self.low = arm.resources.local_memory_low_watermark_mib * 1024 * 1024
        self.high = arm.resources.local_memory_high_watermark_mib * 1024 * 1024
        self.hard = arm.resources.local_memory_capacity_mib * 1024 * 1024
        self._lifecycles: dict[str, tuple[CubeSandboxLifecycle, CubeSandboxLifecycle]] = {}
        self._lock = RLock()
        self._borrow_lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._error: Exception | None = None
        self.high_crossings = 0
        self.high_overshoot_byte_seconds = 0.0
        self.hard_overshoot_byte_seconds = 0.0
        self.high_overshoot_seconds = 0.0
        self.hard_overshoot_seconds = 0.0
        self.peak_local_bytes = 0
        self.peak_shared_live_bytes = 0
        self.peak_combined_live_bytes = 0
        self.peak_shared_total_bytes = 0
        self.borrow_count = 0
        self.borrow_service_seconds = 0.0
        self.return_count = 0
        self.return_release_count = 0
        self.return_service_seconds = 0.0
        self._last_reactive_borrow_local_bytes: int | None = None
        self.memory_scheduling = getattr(arm.resources, 'memory_scheduling', 'static')
        self.borrow_cooldown = getattr(arm.resources, 'borrow_cooldown_seconds', 5.)
        self.return_cooldown = getattr(arm.resources, 'return_cooldown_seconds', 5.)
        self._last_borrow = float('-inf')
        self._last_return = float('-inf')
        self._last_borrow_effect: dict[str, Any] | None = None
        self._last_borrow_rejection: str | None = None
        configured_costs = getattr(arm.resources, 'memory_action_costs', None)
        self.action_cost_model = (
            MemoryActionCostModel(configured_costs) if configured_costs is not None else None
        )

    def register(self, session_id: str, tool: CubeSandboxLifecycle,
                 runtime: CubeSandboxLifecycle) -> None:
        with self._lock:
            self._lifecycles[session_id] = (tool, runtime)

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._lifecycles.pop(session_id, None)

    def start(self) -> None:
        self._thread = Thread(target=self._run, name="memory-watermarks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                raise RuntimeError("memory watermark controller did not stop")
        if self._error is not None:
            raise RuntimeError(f"memory watermark controller failed: {self._error}") from self._error

    def _record_borrow(self, lifecycle: CubeSandboxLifecycle, *, reason: str) -> bool:
        with self._borrow_lock:
            self._last_borrow_rejection = None
            if self.borrower is None or self.memory_scheduling in {'local_only', 'snapshot_only'}:
                self._last_borrow_rejection = 'borrowing_disabled'
                return False
            if (lifecycle.sandbox_id in set(self.borrower.borrowed())
                    and self.borrower.allocates_shared(lifecycle.sandbox_id)):
                return True
            if self.memory_scheduling == 'joint' and time.monotonic()-self._last_borrow < self.borrow_cooldown:
                self._last_borrow_rejection = 'borrow_cooldown'
                return False
            try:
                record = lifecycle.borrow_shared()
                if record is None:
                    self._last_borrow_rejection = 'lifecycle_not_resident'
                    return False
            except WarmCapacityError:
                self._last_borrow_rejection = 'shared_capacity_unavailable'
                return False
            self.borrow_count += 1
            self._last_borrow = time.monotonic()
            self.borrow_service_seconds += record.service_seconds
            self._last_borrow_effect = asdict(record)
            self.events.write({
                "event": "shared_memory_borrowed",
                "role": lifecycle.role,
                "reason": reason,
                **asdict(record),
            })
            return True

    def _decision(self, action: str, reason: str, *,
                  candidates: dict[str, Any] | None = None,
                  estimates: dict[str, Any] | None = None,
                  effect: dict[str, Any] | None = None):
        self.events.write({'event': 'memory_scheduling_decision',
            'compute_node': self.node_id, 'action': action, 'reason': reason,
            'policy': self.memory_scheduling,
            'cost_model': (self.action_cost_model.name if self.action_cost_model
                           else 'uncalibrated_deterministic_fallback'),
            'cost_provenance': (self.action_cost_model.provenance
                                if self.action_cost_model else None),
            'candidates': candidates, 'cost_estimates': estimates,
            'physical_effect': effect,
            'pool': self.shared_pool.snapshot()})

    def _next_borrow_candidate(self) -> CubeSandboxLifecycle | None:
        if self.borrower is None:
            return None
        borrowed = {sandbox_id for sandbox_id in self.borrower.borrowed()
                    if self.borrower.allocates_shared(sandbox_id)}
        with self._lock:
            candidates = [
                lifecycle
                for session in self._lifecycles.values()
                for lifecycle in session
                if lifecycle.resident and lifecycle.sandbox_id not in borrowed
            ]
        ranked = []
        for candidate in candidates:
            observation = candidate.observe_borrow_candidate()
            if observation is not None:  # checkpoint/cleanup may have won the lifecycle lock
                ranked.append((-observation.local_bytes, candidate.sandbox_id or "", candidate))
        return min(ranked, key=lambda item: item[:2])[2] if ranked else None

    def _joint_relieve_pressure(self, *,
                               preferred_borrow: CubeSandboxLifecycle | None = None,
                               reason: str) -> str:
        """Try each safe action once, ordered by calibrated cost when present."""
        self._last_borrow_rejection = None
        borrowing_enabled = (self.borrower is not None
                             and self.memory_scheduling not in {'local_only', 'snapshot_only'})
        if self.action_cost_model is None:
            if self.coordinator.evict_one_for_watermark():
                self._decision('reclaim', reason)
                return 'reclaim'
            borrowed = borrowing_enabled and (
                self._record_borrow(preferred_borrow, reason=reason)
                if preferred_borrow is not None else self._borrow_one()
            )
            action = 'borrow' if borrowed else 'wait'
            self._decision(
                action, self._last_borrow_rejection if action == 'wait' else reason,
                effect=self._last_borrow_effect if action == 'borrow' else None,
            )
            return action

        borrow_candidate = ((preferred_borrow or self._next_borrow_candidate())
                            if borrowing_enabled else None)
        reclaim_candidate_fn = getattr(
            self.coordinator, 'watermark_reclaim_candidate', None,
        )
        reclaim_candidate = (
            reclaim_candidate_fn() if reclaim_candidate_fn is not None else None
        )
        reclaim_tier = 'cold' if self.arm.resources.snapshot_destination == 'cold' else 'warm'
        if reclaim_candidate is not None and reclaim_tier == 'warm':
            pool = self.shared_pool.snapshot()
            warm_usage = (
                int(pool['borrowed_bytes']) + int(pool['reserved_bytes'])
                + int(pool['committed_bytes'])
            )
            required = int(
                reclaim_candidate.get('snapshot_reservation_bytes') or 0
            )
            if required <= 0:
                reclaim_candidate = None
            elif warm_usage + required > int(pool['capacity_bytes']):
                if (self.arm.resources.snapshot_storage == 'tiered'
                        and self.arm.resources.cold_snapshot_root):
                    # A calibrated action must match its cost estimate. Keep
                    # existing WARM generations in place and target COLD
                    # directly instead of triggering an implicit LRU spill.
                    reclaim_tier = 'cold'
                else:
                    reclaim_candidate = None
        borrow_bytes = (
            int(borrow_candidate.configured_memory_bytes or 0)
            if borrow_candidate is not None else None
        )
        reclaim_bytes = (
            int(reclaim_candidate.get('configured_bytes') or 0)
            if reclaim_candidate is not None else None
        )
        chosen, estimates = self.action_cost_model.choose(
            borrow_bytes=borrow_bytes, reclaim_bytes=reclaim_bytes,
            reclaim_tier=reclaim_tier,
        )
        candidates = {
            'borrow': ({'sandbox_id': borrow_candidate.sandbox_id,
                        'role': borrow_candidate.role,
                        'configured_bytes': borrow_bytes}
                       if borrow_candidate is not None else None),
            'reclaim': ({**reclaim_candidate, 'planned_snapshot_tier': reclaim_tier}
                        if reclaim_candidate is not None else None),
        }
        order = [chosen] if chosen is not None else []
        order.extend(action for action in ('reclaim', 'borrow') if action not in order)
        for action in order:
            if action == 'reclaim' and reclaim_candidate is not None:
                if self.coordinator.evict_one_for_watermark(
                    tier=SnapshotTier(reclaim_tier),
                ):
                    self._decision('reclaim', reason, candidates=candidates,
                                   estimates=estimates)
                    return 'reclaim'
            if action == 'borrow' and borrow_candidate is not None:
                if self._record_borrow(borrow_candidate, reason=reason):
                    self._decision('borrow', reason, candidates=candidates,
                                   estimates=estimates,
                                   effect=self._last_borrow_effect)
                    return 'borrow'
        self._decision('wait', self._last_borrow_rejection or reason,
                       candidates=candidates, estimates=estimates)
        return 'wait'

    def route_progress_to_shared(self, lifecycle: CubeSandboxLifecycle,
                                 predicted_increment_mib: int) -> bool:
        """Borrow before an existing Tool's prediction would cross LOCAL HARD."""
        if self.borrower is None or self.memory_scheduling in {'local_only', 'snapshot_only'}:
            return False
        if (lifecycle.sandbox_id in set(self.borrower.borrowed())
                and self.borrower.allocates_shared(lifecycle.sandbox_id)):
            return True
        if not self.coordinator.local_commitment_exceeds(
                predicted_increment_mib,
                self.high if self.memory_scheduling == 'joint' else self.hard):
            return False
        if not getattr(lifecycle, 'resident', True):
            return False
        if self.memory_scheduling == 'joint':
            action = self._joint_relieve_pressure(
                preferred_borrow=lifecycle, reason='predicted_local_high_guard',
            )
            return action == 'borrow'
        # Lack of shared capacity means wait on LOCAL admission, not fabricate
        # shared headroom or invalidate an otherwise healthy execution.
        borrowed = self._record_borrow(lifecycle, reason=(
            'predicted_local_high_guard' if self.memory_scheduling == 'joint'
            else 'predicted_local_hard_guard'))
        return borrowed

    def _borrow_one(self) -> bool:
        candidate = self._next_borrow_candidate()
        return bool(candidate is not None and self._record_borrow(candidate, reason=(
            'local_high_or_blocked_demand' if self.memory_scheduling == 'joint'
            else 'observed_local_hard_guard')))

    def _return_one(self, local_used_bytes: int) -> bool:
        """Return one VM only when observed shared pages fit below LOCAL HIGH."""
        if self.borrower is None or self.memory_scheduling != 'joint':
            return False
        now = time.monotonic()
        if now - max(self._last_borrow, self._last_return) < self.return_cooldown:
            return False
        borrowed = set(self.borrower.borrowed())
        if not borrowed:
            return False
        with self._lock:
            lifecycles = {
                lifecycle.sandbox_id: (session_id, lifecycle)
                for session_id, session in self._lifecycles.items()
                for lifecycle in session
                if lifecycle.resident and lifecycle.sandbox_id in borrowed
            }
        candidates = []
        for sandbox_id, (session_id, lifecycle) in lifecycles.items():
            # Observe through the lifecycle lock.  A session can finish and
            # remove its cgroup after the registry snapshot above; in that
            # case it is no longer a return candidate, not a controller-wide
            # failure.
            observation = lifecycle.observe_borrow_candidate()
            if observation is None:
                continue
            # A residual shared page keeps the capacity reservation alive, but
            # a VM already rebound to LOCAL is not another return candidate.
            if observation.effective_mems != str(self.borrower.shared_node):
                continue
            if local_used_bytes + observation.shared_bytes <= self.high:
                candidates.append((observation.shared_bytes, sandbox_id,
                                   session_id, lifecycle, observation))
        if not candidates:
            return False
        record = None
        before = None
        lifecycle = None
        for _shared, _sandbox_id, session_id, candidate, observation in sorted(candidates):
            coordinate_tool = candidate.role == "tool"
            if coordinate_tool and not self.coordinator.claim_memory_return(session_id):
                continue
            try:
                record = candidate.return_local_memory()
            finally:
                if coordinate_tool:
                    self.coordinator.release_memory_return(session_id)
            if record is not None:
                lifecycle = candidate
                before = observation
                break
        if record is None or lifecycle is None or before is None:
            return False
        self.return_count += 1
        self.return_release_count += int(record.reservation_released)
        self.return_service_seconds += record.service_seconds
        self._last_return = time.monotonic()
        effect = asdict(record)
        effect['eligibility_observation'] = asdict(before)
        self.events.write({
            'event': 'shared_memory_returned', 'role': lifecycle.role,
            'reason': 'local_low_hysteresis', **asdict(record),
        })
        self._decision('return', 'local_low_hysteresis', effect=effect)
        return True

    def _run(self) -> None:
        previous = time.monotonic()
        above_high = False
        try:
            while not self._stop.wait(self.sampler.interval_s):
                now = time.monotonic()
                elapsed = max(0.0, now - previous)
                previous = now
                above_high = self._sample_once(elapsed, above_high)
        except Exception as exc:
            self._error = exc
            try:
                self.events.write({
                    'event': 'memory_watermark_controller_failed',
                    'compute_node': self.node_id,
                    'error_type': type(exc).__name__,
                    'error': str(exc),
                })
            except Exception:
                pass

    def _sample_once(self, elapsed: float, above_high: bool) -> bool:
        """Apply one deterministic watermark decision; split out for audit tests."""
        local, shared, combined = self.sampler.tier_usage()
        pool = self.shared_pool.snapshot()
        snapshot_claim = int(pool["reserved_bytes"]) + int(pool["committed_bytes"])
        shared_total = shared + snapshot_claim
        self.peak_local_bytes = max(self.peak_local_bytes, local)
        self.peak_shared_live_bytes = max(self.peak_shared_live_bytes, shared)
        self.peak_combined_live_bytes = max(self.peak_combined_live_bytes, combined)
        self.peak_shared_total_bytes = max(self.peak_shared_total_bytes, shared_total)
        if shared_total > int(pool["capacity_bytes"]):
            raise RuntimeError(
                "shared NUMA physical capacity exceeded: "
                f"live={shared} snapshot_claim={snapshot_claim} "
                f"capacity={pool['capacity_bytes']}"
            )
        self.high_overshoot_byte_seconds += max(0, local - self.high) * elapsed
        self.hard_overshoot_byte_seconds += max(0, local - self.hard) * elapsed
        if local > self.high:
            self.high_overshoot_seconds += elapsed
        if local > self.hard:
            self.hard_overshoot_seconds += elapsed
        if local >= self.high and not above_high:
            above_high = True
            self.coordinator.set_new_session_admission_blocked(True)
            self.high_crossings += 1
            self.events.write({
                "event": "local_high_watermark_crossed", "compute_node": self.node_id,
                "local_used_bytes": local, "shared_live_used_bytes": shared,
                "combined_live_used_bytes": combined,
            })
        if local <= self.low and above_high:
            above_high = False
            self.coordinator.set_new_session_admission_blocked(False)
            self.events.write({
                "event": "local_low_watermark_reached", "compute_node": self.node_id,
                "local_used_bytes": local, "shared_live_used_bytes": shared,
            })
        blocked_demand = (
            self.coordinator.blocked_demand_pressure()
            if self.memory_scheduling == 'joint' else False
        )
        if local <= self.low and not above_high and not blocked_demand:
            if self._return_one(local):
                return above_high
        joint_pressure = self.memory_scheduling == 'joint' and (
            above_high or blocked_demand)
        if joint_pressure:
            self._joint_relieve_pressure(reason=(
                'local_high_or_blocked_demand' if above_high
                else 'blocked_demand_below_high'
            ))
            return above_high
        if above_high and self.arm.policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE:
            if self._joint_relieve_pressure(reason="local_high_watermark") != "wait":
                return above_high
        if self.memory_scheduling in {'local_only', 'snapshot_only'}:
            return above_high
        if local >= self.hard:
            # cpuset rebinding redirects future faults but can leave existing
            # pages on LOCAL. Borrow again only
            # when LOCAL keeps growing, not every 200 ms while the same pages
            # remain charged on the original compute node.
            growth_step = 64 * 1024 * 1024
            if (self._last_reactive_borrow_local_bytes is None
                    or local >= self._last_reactive_borrow_local_bytes + growth_step):
                if not self._borrow_one():
                    raise RuntimeError("LOCAL hard watermark reached with no borrowable VM")
                self._last_reactive_borrow_local_bytes = local
        return above_high


def _network_target(endpoint: str, *, label: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} must be an http(s) URL with a hostname")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return parsed.hostname
    return f"{address}/{32 if address.version == 4 else 128}"


def _runtime_network_deny_out(
    allow_internet_access: bool, allow_out: list[str],
) -> list[str] | None:
    """Deny default IPv4 egress only for explicitly closed Runtime arms."""
    return ["0.0.0.0/0"] if allow_out and not allow_internet_access else None


class RuntimeStatusPoller:
    """Poll a detached Runtime without coupling resident policy to callbacks."""

    def __init__(self, *, lifecycle: CubeSandboxLifecycle,
                 executor: CubeCommandExecutor, lifecycle_lock: Lock,
                 snapshot_enabled: bool) -> None:
        self.lifecycle = lifecycle
        self.executor = executor
        self.lifecycle_lock = lifecycle_lock
        self.snapshot_enabled = snapshot_enabled

    def __call__(self, command: str, timeout: float) -> CommandResult | None:
        if not self.snapshot_enabled:
            if not self.lifecycle.resident:
                return None
            return _execute_idempotent(self.executor, command, timeout)
        # Snapshot policies serialize a bounded status command with the state
        # transition. Resident policies have no transition to serialize and
        # must leave the lock available to the ModelGateway callback.
        with self.lifecycle_lock:
            if not self.lifecycle.resident:
                return None
            return _execute_idempotent(self.executor, command, timeout)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def atomic_marker(path: Path, digest: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="ascii") as stream:
        stream.write(digest + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _make_time_span(
    name: str, start: Any, end: Any, *, role: str | None = None,
    operation: str | None = None, execution_id: str | None = None,
    start_monotonic: Any = None, end_monotonic: Any = None,
    service_seconds: Any = None, status: str | None = None,
    state_before: str | None = None, state_after: str | None = None,
    error_type: str | None = None, predicted_wait_seconds: Any = None,
    prediction_source: str | None = None,
    execution_scope: str | None = None,
) -> dict[str, Any] | None:
    """Render one span with wall and, when available, monotonic timing.

    Wall-clock fields make JSONL/results easy to correlate across processes;
    monotonic fields keep durations valid when the system clock is adjusted.
    Optional lifecycle fields are retained only when they are well-typed so a
    malformed observer record cannot make a result look like valid evidence.
    """
    if (
        isinstance(start, bool) or not isinstance(start, (int, float))
        or isinstance(end, bool) or not isinstance(end, (int, float))
        or not math.isfinite(float(start)) or not math.isfinite(float(end))
    ):
        return None
    start_wall = float(start)
    end_wall = float(end)
    start_mono = (
        float(start_monotonic)
        if isinstance(start_monotonic, (int, float))
        and not isinstance(start_monotonic, bool)
        and math.isfinite(float(start_monotonic))
        else None
    )
    end_mono = (
        float(end_monotonic)
        if isinstance(end_monotonic, (int, float))
        and not isinstance(end_monotonic, bool)
        and math.isfinite(float(end_monotonic))
        else None
    )
    duration = (
        max(0.0, end_mono - start_mono)
        if start_mono is not None and end_mono is not None
        else max(0.0, end_wall - start_wall)
    )
    span: dict[str, Any] = {
        "name": str(name),
        "start_unix_s": start_wall,
        "end_unix_s": end_wall,
        "duration_seconds": duration,
    }
    optional = {
        "role": role,
        "operation": operation,
        "execution_id": execution_id,
        "execution_scope": execution_scope,
        "start_monotonic_s": start_mono,
        "end_monotonic_s": end_mono,
        "service_seconds": service_seconds,
        "status": status,
        "state_before": state_before,
        "state_after": state_after,
        "error_type": error_type,
        "predicted_wait_seconds": predicted_wait_seconds,
        "prediction_source": prediction_source,
    }
    for key, value in optional.items():
        if value is not None:
            span[key] = value
    return span


def _record_time_span(timeline: dict[str, Any], name: str, start: Any, end: Any,
                      **kwargs: Any) -> None:
    span = _make_time_span(name, start, end, **kwargs)
    if span is not None:
        timeline.setdefault("recorded_time_spans", []).append(span)


def observed_tool_concurrency(sessions, *, execution_scope=None):
    """Sweep completed Tool operation intervals, not offered Agent counts.

    Timestamps originate in Runtime VMs; clock synchronization remains a
    prerequisite for cross-VM overlap. Failed sessions may lack complete spans.
    """
    edges = []
    for session in sessions:
        for span in session.get('timeline', {}).get('recorded_time_spans', []):
            if span['name'] != 'tool.operation':
                continue
            if execution_scope is not None and span.get('execution_scope') != execution_scope:
                continue
            start, end = span['start_unix_s'], span['end_unix_s']
            if end > start:
                edges.extend([(start, 1), (end, -1)])
    active = peak = 0
    area = 0.
    previous = min((t for t, _ in edges), default=0.)
    for timestamp, delta in sorted(edges):
        area += active * (timestamp-previous)
        active += delta
        peak = max(peak, active)
        previous = timestamp
    window = max((t for t, _ in edges), default=0.)-min((t for t, _ in edges), default=0.)
    return {'peak': peak if edges else None, 'mean_over_tool_window': area/window if window else None,
            'interval_count': len(edges)//2, 'scope': 'completed_tool_operation_intervals',
            'execution_scope': execution_scope or 'all_admitted_operations_including_backend_maintenance',
            'clock_requirement': 'synchronized_runtime_vm_wall_clocks'}


def _execute_idempotent(executor: Any, command: str, timeout_s: float,
                        *, attempts: int = 8) -> CommandResult:
    """Retry read-only/setup Cube command streams during high fan-out.

    Agent Tool commands never use this helper: retrying those could duplicate a
    workspace mutation.  It is reserved for idempotent readiness, validation,
    hashing, PID observation, and artifact-copy operations.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return executor.execute(command, timeout_s)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(0.1 * (2 ** (attempt - 1)), 1.0))
    raise RuntimeError(
        f"idempotent Cube command transport failed after {attempts} attempts"
    ) from last_error


def build_time_spans(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    """Build stable, machine-readable spans from session lifecycle markers."""
    pairs = (
        ("session", "session_started", "session_finished"),
        ("sandbox.create.queue", "sandbox_create_gate_wait_start",
         "sandbox_create_gate_acquired"),
        ("sandbox.create", "sandbox_create_start", "sandbox_ready"),
        ("sandbox.tool.create", "tool_create_start", "tool_ready"),
        ("sandbox.runtime.create", "runtime_create_start", "runtime_ready"),
        ("agent", "agent_execution_start", "final_agent_completion"),
        ("validation", "validation_start", "validation_end"),
        ("output_hash", "output_hash_start", "output_hash_end"),
        ("sandbox.cleanup", "sandbox_cleanup_start", "sandbox_cleanup_end"),
    )
    spans: list[dict[str, float | str]] = []
    for name, start_key, end_key in pairs:
        start = timeline.get(start_key)
        end = timeline.get(end_key)
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        span = _make_time_span(name, start, end)
        if span is not None:
            spans.append(span)
    for item in timeline.get("recorded_time_spans", []):
        if isinstance(item, dict) and item.get("name"):
            spans.append(dict(item))
    for item in timeline.get("lifecycle_timings", []):
        if not isinstance(item, dict):
            continue
        operation = str(item.get("operation") or "").strip()
        if not operation:
            continue
        role = str(item.get("role") or "sandbox").strip() or "sandbox"
        span = _make_time_span(
            f"sandbox.{role}.{operation}",
            item.get("started_unix_s"), item.get("completed_unix_s"),
            role=role, operation=operation,
            start_monotonic=item.get("started_monotonic_s"),
            end_monotonic=item.get("completed_monotonic_s"),
            service_seconds=item.get("service_seconds"),
            status=item.get("status"), state_before=item.get("state_before"),
            state_after=item.get("state_after"), error_type=item.get("error_type"),
        )
        if span is not None:
            for key in (
                "host_memory_before", "host_memory_after",
                "host_observed_reclaimed_bytes", "host_observed_growth_bytes",
            ):
                if item.get(key) is not None:
                    span[key] = item[key]
            spans.append(span)
    return spans


def enrich_tool_execution_observations(
    records: list[dict[str, Any]],
    cgroup_artifacts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach validated guest execution measurements to admission records."""
    expected_ids = {str(item.get("execution_id") or "") for item in records}
    if "" in expected_ids or expected_ids != set(cgroup_artifacts):
        raise RuntimeError(
            "prediction/cgroup execution identity mismatch: "
            f"predictions={sorted(expected_ids)!r} "
            f"cgroups={sorted(cgroup_artifacts)!r}"
        )
    enriched: list[dict[str, Any]] = []
    for item in records:
        execution_id = str(item["execution_id"])
        cgroup = cgroup_artifacts[execution_id]
        actual_mib = float(cgroup["memory_rss_peak_bytes"]) / (1024.0 * 1024.0)
        duration = max(0.0, float(cgroup["ts_end"]) - float(cgroup["ts_start"]))
        row = dict(item)
        row.update({
            "actual_measured_memory_mib": actual_mib,
            "actual_memory_metric": "tool_guest_cgroup_v2_memory_rss_peak_bytes",
            "actual_execution_duration_seconds": duration,
            "actual_cpu_utilization_avg_cores": float(
                cgroup["cpu_utilization_avg_cores"]
            ),
            "telemetry_validity": "valid",
            "telemetry_eligible_for_kb": True,
        })
        if cgroup.get("memory_eligible") is True and cgroup.get("memory_measurement") == "guest_memtotal_minus_memavailable":
            extra = cgroup.get("memory_extra_peak_bytes")
            if isinstance(extra, (int, float)) and math.isfinite(extra) and extra >= 0:
                row["actual_extra_memory_mib"] = extra / 1024**2
                if row.get("prediction_quantile") == 0.5:
                    value = row.get("predicted_guest_extra_memory_mib")
                    if value is not None:
                        row["p50_extra_memory_error_mib"] = float(value) - extra / 1024**2
        row["memory_unavailable_reason"] = cgroup.get("memory_unavailable_reason")
        predicted_guest = row.get("predicted_guest_memory_p90_mib")
        if predicted_guest is not None:
            predicted_mib = float(predicted_guest)
            signed_error = predicted_mib - actual_mib
            row.update({
                "prediction_error_mib": signed_error,
                "prediction_absolute_error_mib": abs(signed_error),
                "prediction_underestimate_mib": max(0.0, -signed_error),
                "prediction_covered_actual": predicted_mib >= actual_mib,
            })
        predicted_host = row.get("predicted_incremental_memory_mib")
        actual_host = row.get("actual_host_execution_increment_mib")
        if (predicted_host is not None and actual_host is not None
                and row.get("admission_prediction_target") == "host_cgroup_memory_current_increment"):
            host_error = float(predicted_host) - float(actual_host)
            row.update({
                "host_increment_prediction_error_mib": host_error,
                "host_increment_prediction_absolute_error_mib": abs(host_error),
                "host_increment_prediction_covered_actual": host_error >= 0,
            })
        enriched.append(row)
    records[:] = enriched
    return enriched


def summarize_tool_execution_observations(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Produce paper-facing prediction and telemetry aggregates."""
    # Backend probes are admitted and measured, but are not logical Agent Tool
    # calls and must not dilute command-prediction statistics.
    records = [
        item for item in records
        if item.get("execution_scope", "agent-tool") == "agent-tool"
    ]
    errors = [
        float(item["prediction_error_mib"])
        for item in records if item.get("prediction_error_mib") is not None
    ]
    absolute_errors = [
        float(item["prediction_absolute_error_mib"])
        for item in records if item.get("prediction_absolute_error_mib") is not None
    ]
    underestimates = [
        float(item["prediction_underestimate_mib"])
        for item in records if item.get("prediction_underestimate_mib") is not None
    ]
    prediction_records = [
        item for item in records
        if item.get("prediction_source") == "runtime_clawtune_immutable_kb"
    ]
    fallback_records = [
        item for item in prediction_records
        if item.get("fallback_level") not in {"exact_command", "lattice"}
    ]
    covered = [
        bool(item["prediction_covered_actual"])
        for item in records if item.get("prediction_covered_actual") is not None
    ]
    relative_errors = [
        float(item["prediction_absolute_error_mib"])
        / float(item["actual_measured_memory_mib"])
        for item in records
        if item.get("prediction_absolute_error_mib") is not None
        and float(item.get("actual_measured_memory_mib") or 0) > 0
    ]
    over_reservation_ratios = [
        float(item["admitted_reservation_mib"])
        / float(item["actual_host_execution_increment_mib"])
        for item in records
        if item.get("admitted_reservation_mib") is not None
        and float(item.get("actual_host_execution_increment_mib") or 0) > 0
    ]
    pinball_losses = []
    for item in records:
        predicted = item.get("predicted_guest_memory_p90_mib")
        actual = item.get("actual_measured_memory_mib")
        if predicted is None or actual is None:
            continue
        residual = float(actual) - float(predicted)
        pinball_losses.append(0.9 * residual if residual >= 0 else -0.1 * residual)
    host_coverage = [
        bool(item["host_increment_prediction_covered_actual"])
        for item in records
        if item.get("host_increment_prediction_covered_actual") is not None
    ]

    def distribution(field: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in records:
            value = str(item.get(field) or "unknown")
            result[value] = result.get(value, 0) + 1
        return result

    return {
        "p50_extra_memory_measured_count": sum(r.get("p50_extra_memory_error_mib") is not None for r in records),
        "p50_extra_memory_mae_mib": (statistics.fmean(abs(r["p50_extra_memory_error_mib"]) for r in records
            if r.get("p50_extra_memory_error_mib") is not None) if any(r.get("p50_extra_memory_error_mib") is not None for r in records) else None),
        "p50_source_counts": distribution("fallback_level"),
        "tool_execution_observation_count": len(records),
        "telemetry_valid_count": sum(
            item.get("telemetry_validity") == "valid" for item in records
        ),
        "telemetry_invalid_count": sum(
            item.get("telemetry_validity") != "valid" for item in records
        ),
        "prediction_observation_count": len(prediction_records),
        "prediction_fallback_count": len(fallback_records),
        "prediction_fallback_rate": (
            len(fallback_records) / len(prediction_records)
            if prediction_records else None
        ),
        "prediction_error_mean_mib": statistics.fmean(errors) if errors else None,
        "prediction_error_p90_mib": percentile(errors, 0.90),
        "prediction_absolute_error_p90_mib": percentile(absolute_errors, 0.90),
        "prediction_underestimate_p90_mib": percentile(underestimates, 0.90),
        "prediction_coverage_fraction": (
            sum(covered) / len(covered) if covered else None
        ),
        "prediction_exceedance_rate": (
            1.0 - sum(covered) / len(covered) if covered else None
        ),
        "prediction_relative_absolute_error_mean": (
            statistics.fmean(relative_errors) if relative_errors else None
        ),
        "reservation_over_actual_mean_ratio": (
            statistics.fmean(over_reservation_ratios)
            if over_reservation_ratios else None
        ),
        "prediction_p90_pinball_loss_mean_mib": (
            statistics.fmean(pinball_losses) if pinball_losses else None
        ),
        "host_increment_prediction_coverage_fraction": (
            sum(host_coverage) / len(host_coverage) if host_coverage else None
        ),
        "host_increment_prediction_exceedance_rate": (
            1.0 - sum(host_coverage) / len(host_coverage)
            if host_coverage else None
        ),
        "prediction_source_distribution": distribution("prediction_source"),
        "prediction_fallback_level_distribution": distribution("fallback_level"),
    }


class EventWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = Lock()
        self.sequence = 0

    def write(self, event: dict[str, Any]) -> None:
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            row = {
                "schema_version": 1,
                "sequence_no": self.sequence,
                "wall_time": utcnow().isoformat(),
                "wall_time_ns": str(time.time_ns()),
                "monotonic_time_ns": str(time.monotonic_ns()),
                **event,
            }
            self.sequence += 1
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()


class ExperimentWorker:
    def __init__(self, spec: ExperimentSpec, *, run_id: str, attempt_id: str,
                 task_uid: str, output_root: Path | None = None,
                 client: CubeSandboxClient | None = None) -> None:
        self.spec = spec
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.task_uid = task_uid
        output_base = Path(os.environ.get("CLAWBOX_OUTPUT_ROOT", spec.output.directory))
        self.output_root = output_root or output_base / run_id
        journal = OwnedSandboxJournal(self.output_root / "owned-sandboxes.jsonl")
        self.client = client or CubeSandboxClient(journal=journal)
        if self.client.journal is None:
            self.client.journal = journal
        self.results: list[ResultEnvelope] = []
        self.policy_control: PolicyControlServer | None = None
        self.model_gateway: ManagedModelGateway | None = None

    @staticmethod
    def _sandbox_create_limit(concurrency: int) -> int:
        """Bound pair creation so c40 does not stampede Cubelet/containerd."""
        raw = os.environ.get("CLAWBOX_SANDBOX_CREATE_CONCURRENCY", "8")
        try:
            limit = int(raw)
        except ValueError as exc:
            raise ValueError(
                "CLAWBOX_SANDBOX_CREATE_CONCURRENCY must be a positive integer"
            ) from exc
        if limit < 1:
            raise ValueError(
                "CLAWBOX_SANDBOX_CREATE_CONCURRENCY must be a positive integer"
            )
        return min(concurrency, limit)

    @staticmethod
    def _startup_headroom_mib(
        arm: ExperimentArm,
        prediction_provider: P50PredictionProvider | None,
    ) -> int:
        """Identical lifecycle margin in every admission ablation arm."""
        return arm.resources.startup_headroom_mib

    def run(self, *, arm_ids: set[str] | None = None,
            write_summary: bool = True) -> list[ResultEnvelope]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        control_host = os.environ.get("CLAWBOX_CONTROL_HOST", "").strip()
        policy_port = int(os.environ.get("CLAWBOX_POLICY_PORT", "18080"))
        gateway_host = os.environ.get("CLAWBOX_MODEL_GATEWAY_HOST", control_host).strip()
        gateway_port = int(os.environ.get("CLAWBOX_MODEL_GATEWAY_PORT", "18081"))
        arms = list(expand_matrix(self.spec))
        if arm_ids is not None:
            known = {arm.arm_id for arm in arms}
            unknown = arm_ids - known
            if unknown:
                raise ValueError(f"unknown experiment arm ids: {sorted(unknown)}")
            arms = [arm for arm in arms if arm.arm_id in arm_ids]
            if not arms:
                raise ValueError("at least one experiment arm must be selected")
        requires_control = any(arm.agent.driver is AgentDriver.OPENCLAW for arm in arms)
        if requires_control and not control_host:
            raise RuntimeError("native SSH policy control requires CLAWBOX_CONTROL_HOST")
        if requires_control and not gateway_host:
            raise RuntimeError(
                "managed ModelGateway requires CLAWBOX_MODEL_GATEWAY_HOST and "
                "CLAWBOX_MODEL_GATEWAY_NODE_PORT"
            )
        policy_context = (
            PolicyControlServer(advertise_host=control_host, advertised_port=policy_port,
                                bind_port=policy_port)
            if control_host else nullcontext(None)
        )
        gateway_context = (
            ManagedModelGateway(advertise_host=gateway_host, advertised_port=gateway_port,
                                bind_port=gateway_port)
            if gateway_host else nullcontext(None)
        )
        with policy_context as policy_control, gateway_context as model_gateway:
            self.policy_control = policy_control
            self.model_gateway = model_gateway
            if model_gateway is not None:
                model_gateway.wait_ready()
            for arm in arms:
                result_path = self.output_root / "arms" / f"{arm.arm_id}.json"
                marker_path = self.output_root / "arms" / f"{arm.arm_id}.complete"
                if self._completed(result_path, marker_path, arm.spec_digest):
                    self.results.append(ResultEnvelope.model_validate_json(result_path.read_text()))
                    continue
                self.results.append(self._run_arm(arm, result_path, marker_path))
            self.policy_control = None
            self.model_gateway = None
        if write_summary:
            self._write_summary()
        return self.results

    @staticmethod
    def _completed(result_path: Path, marker_path: Path, digest: str) -> bool:
        if not result_path.exists() or not marker_path.exists():
            return False
        try:
            previous = json.loads(result_path.read_text(encoding="utf-8"))
            return marker_path.read_text(encoding="ascii").strip() == digest and \
                previous["arm"]["spec_digest"] == digest and \
                previous["status"] == RunStatus.SUCCEEDED
        except (OSError, ValueError, KeyError):
            return False

    def _run_arm(self, arm: ExperimentArm, result_path: Path, marker_path: Path) -> ResultEnvelope:
        started_at = utcnow()
        started = time.monotonic()
        session_cases = arm.session_cases or (arm.case,)
        tool_sandboxes = {
            (case.sandbox or arm.sandbox).template: case.sandbox or arm.sandbox
            for case in session_cases
        }
        for role, sandbox in (("runtime", arm.runtime), *(
            (f"tool:{template}", sandbox)
            for template, sandbox in tool_sandboxes.items()
        )):
            if sandbox.image_digest:
                self.client.validate_template_image(
                    sandbox.template, sandbox.image_digest,
                )
        events = EventWriter(self.output_root / "events" / f"{arm.arm_id}.jsonl")
        shared_borrow_mib = arm.resources.shared_memory_borrow_limit_mib
        sampler = (
            NumaCgroupMemorySampler(
                Path(arm.resources.local_memory_cgroup),
                local_capacity_bytes=arm.resources.local_memory_capacity_mib * 1024 * 1024,
                total_capacity_bytes=(arm.resources.physical_live_limit_mib or (
                    arm.resources.local_memory_capacity_mib + (shared_borrow_mib or 0)
                )) * 1024 * 1024,
                local_numa_node=(arm.resources.compute_nodes[0].numa_node
                                 if arm.resources.compute_nodes else arm.resources.local_numa_node),
                local_numa_nodes=tuple(n.numa_node for n in arm.resources.compute_nodes),
                shared_numa_node=arm.resources.warm_numa_node,
                interval_s=arm.execution.memory_sample_interval_seconds,
            )
            if arm.resources.local_memory_cgroup and (shared_borrow_mib is not None or arm.resources.compute_nodes
                or (arm.resources.local_numa_node is not None and arm.resources.local_memory_capacity_mib is not None)) else
            CgroupMemorySampler(
                Path(arm.resources.local_memory_cgroup),
                capacity_bytes=(arm.resources.local_memory_capacity_mib or
                                arm.resources.pool_memory_budget_mib) * 1024 * 1024,
                numa_node=arm.resources.local_numa_node,
                physical_limit_bytes=(arm.resources.physical_live_limit_mib * 1024**2 if arm.resources.physical_live_limit_mib else None),
                interval_s=arm.execution.memory_sample_interval_seconds,
            )
            if arm.resources.local_memory_cgroup else
            NumaNodeMemorySampler(
                arm.resources.local_numa_node,
                interval_s=arm.execution.memory_sample_interval_seconds,
            )
            if arm.resources.local_numa_node is not None and os.name != "nt"
            else NodeMemorySampler(interval_s=arm.execution.memory_sample_interval_seconds)
        )
        def record_pressure_pause(
            state: Any, tool_elapsed: float | None,
            runtime_elapsed: float | None, reason: str,
        ) -> None:
            for role, lifecycle, elapsed in (
                ("tool", state.lifecycle, tool_elapsed),
                ("runtime", state.runtime_lifecycle, runtime_elapsed),
            ):
                if elapsed is None or lifecycle is None:
                    continue
                timings = lifecycle.timings
                events.write({
                    "event": "sandbox_paused",
                    "session_id": state.session_id,
                    "role": role,
                    "service_seconds": elapsed,
                    "reason": reason,
                    "request_id": state.wait_id,
                    "snapshot_tier": lifecycle.tier.value,
                    "lifecycle_timing": timings[-1] if timings else None,
                })
        def reclaim_local_cache() -> None:
            events.write({"event": "local_cache_reclaim_started"})
            try:
                observation = sampler.reclaim_file_cache()
            except OSError as exc:
                events.write({
                    "event": "local_cache_reclaim",
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                raise RuntimeError('LOCAL cache reclamation failed') from exc
            events.write({"event": "local_cache_reclaim", **observation})

        prediction_providers = {}
        if arm.resources.prediction_artifact and (arm.policy.admission is AdmissionPolicy.PREDICTED or arm.resources.observe_predictions):
            for case in session_cases:
                tool_sandbox = case.sandbox or arm.sandbox
                prediction_providers[case.case_id] = prediction_provider_type(arm.resources.prediction_mode)(
                    Path(arm.resources.prediction_artifact),
                    repository=case.repository or case.case_id,
                    sandbox_identity={
                        k: getattr(tool_sandbox, k)
                        for k in (
                            "template_id", "image_digest", "vcpu", "memory_mib", "architecture",
                        )
                    },
                )
        prediction_provider = next(iter(prediction_providers.values()), None)
        global_sampler = CgroupMemorySampler(Path(arm.resources.local_memory_cgroup),
            physical_limit_bytes=(arm.resources.physical_live_limit_mib * 1024**2 if arm.resources.physical_live_limit_mib else None),
            capacity_bytes=((arm.resources.local_memory_capacity_mib or arm.resources.pool_memory_budget_mib)
                            + (shared_borrow_mib or 0))*1024**2)
        demand_ledger = DemandLedger(sample=global_sampler.current, capacity=global_sampler.total,
                                    free_floor=arm.resources.emergency_free_memory_mib*1024**2,
                                    margin=arm.resources.admission_margin_mib*1024**2,
                                    cgroup=Path(arm.resources.local_memory_cgroup))
        snapshot_pool = WarmSnapshotPool(
            arm.resources.warm_memory_capacity_mib * 1024 * 1024,
            borrow_capacity_bytes=(shared_borrow_mib or 0) * 1024 * 1024,
            transfer_concurrency=arm.resources.pool_transfer_concurrency,
            transfer_timeout_seconds=arm.resources.pool_transfer_timeout_seconds,
            snapshot_aware=arm.policy.snapshot_aware_residency,
        )
        domains = {}
        os_default = arm.execution.placement_policy == 'os_default'
        for node in (() if os_default else arm.resources.compute_nodes) or (None,):
            domain_arm = arm
            domain_sampler = sampler
            if node is not None:
                domain_arm = arm.model_copy(update={"resources": arm.resources.model_copy(update={
                    "compute_nodes": (), "local_numa_node": node.numa_node,
                    "local_memory_capacity_mib": node.memory_capacity_mib,
                    "local_memory_low_watermark_mib": node.low_watermark_mib,
                    "local_memory_high_watermark_mib": node.high_watermark_mib,
                    "pool_memory_budget_mib": node.high_watermark_mib,
                })})
                domain_sampler = NumaCgroupMemorySampler(
                    Path(arm.resources.local_memory_cgroup),
                    local_capacity_bytes=node.memory_capacity_mib * 1024 * 1024,
                    total_capacity_bytes=(arm.resources.physical_live_limit_mib or (arm.resources.local_memory_capacity_mib + (shared_borrow_mib or 0))) * 1024 * 1024,
                    local_numa_node=node.numa_node, shared_numa_node=arm.resources.warm_numa_node,
                    interval_s=arm.execution.memory_sample_interval_seconds,
                )
            coordinator = PolicyCoordinator(
                arm.policy, demand_ledger=demand_ledger, demand_scope=node.node_id if node else "local",
                budget_mib=domain_arm.resources.pool_memory_budget_mib,
                emergency_free_mib=arm.resources.emergency_free_memory_mib,
                operation_headroom_mib=policy_operation_headroom_mib(domain_arm),
                progress_budget_mib=policy_progress_budget_mib(domain_arm),
                startup_headroom_mib=self._startup_headroom_mib(domain_arm, prediction_provider),
                os_managed_lifecycle=domain_arm.resources.lifecycle_admission == "os-managed",
                physical_sample=domain_sampler.current,
                reclaim_cache=(reclaim_local_cache if isinstance(sampler, CgroupMemorySampler)
                               and arm.resources.proactive_cache_reclaim else None),
                on_pressure_pause=record_pressure_pause,
            )
            numa_borrower = (
                SandboxNumaBorrower(
                    Path(arm.resources.local_memory_cgroup),
                    local_node=domain_arm.resources.local_numa_node,
                    shared_node=arm.resources.warm_numa_node, shared_pool=snapshot_pool,
                    cpus=node.cpus if node else None, node_id=node.node_id if node else None,
                    cleanup_timeout_seconds=arm.resources.pool_transfer_timeout_seconds,
                ) if shared_borrow_mib is not None else None
            )
            controller = (
                WatermarkController(domain_arm, coordinator, domain_sampler, numa_borrower, events,
                                    shared_pool=snapshot_pool, node_id=node.node_id if node else None)
                if isinstance(domain_sampler, NumaCgroupMemorySampler)
                and domain_arm.resources.local_memory_low_watermark_mib is not None
                and domain_arm.resources.local_memory_high_watermark_mib is not None else None
            )
            if controller is not None:
                coordinator.pressure_relief = (
                    lambda controller=controller: controller._joint_relieve_pressure(
                        reason="memory_admission_or_model_wait") != "wait"
                )
            domains[node.node_id if node else "local"] = SimpleNamespace(
                arm=domain_arm, sampler=domain_sampler, coordinator=coordinator, cpus=node.cpus if node else None,
                borrower=numa_borrower, controller=controller, peak_bytes=0,
            )
        if os_default and arm.resources.compute_nodes:
            # One admission budget and the inherited parent cpuset, with no
            # per-VM CPU or memory-node narrowing in B0.
            domains['local'].cpus = ','.join(n.cpus for n in arm.resources.compute_nodes)
        def session_domain(index):
            if not arm.resources.compute_nodes or os_default:
                return domains["local"]
            node = place_session(arm.resources.compute_nodes, index, arm.execution.session_compute_nodes)
            return domains[node.node_id]
        placement = None
        cpu_sampler = None
        cpu_unavailable_reason = None
        if arm.resources.local_memory_cgroup:
            cpu_cgroup = Path(arm.resources.local_memory_cgroup)
            try:
                cpu_sampler = CgroupCpuSampler(cpu_cgroup)
            except (OSError, ValueError, RuntimeError) as exc:
                cpu_unavailable_reason = str(exc)
        if arm.resources.local_memory_cgroup:
            def record_memory_sample(used: int, available: int) -> None:
                for domain in domains.values():
                    domain.peak_bytes = max(domain.peak_bytes, domain.sampler.current()[0])
                pool = snapshot_pool.snapshot()
                topology_changes = (
                    self._priority_domain.sample_topology_placement()
                    if (self._priority_domain is not None
                        and arm.execution.tool_call_cpu_policy == 'c2') else []
                )
                warm_physical = None
                if arm.resources.warm_memory_capacity_mib and arm.resources.warm_snapshot_root:
                    stat = os.statvfs(arm.resources.warm_snapshot_root)
                    warm_physical = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
                events.write({
                    "event": "memory_sample", "local_used_bytes": used,
                    "cgroup_memory": cgroup_memory_evidence(Path(arm.resources.local_memory_cgroup)),
                    "numa_residency": sampler.node_usage() if isinstance(sampler, NumaCgroupMemorySampler) else None,
                    "combined_live_used_bytes": sampler.tier_usage()[2] if isinstance(sampler, NumaCgroupMemorySampler) else used,
                    "host_available_bytes": available,
                    "host_used_bytes": sampler.host_mem_total - available,
                    "host_used_delta_bytes": (
                        sampler.host_mem_total - available
                        - sampler.host_baseline_used
                    ),
                    "shared_live_used_bytes": (
                        sampler.tier_usage()[1]
                        if isinstance(sampler, NumaCgroupMemorySampler) else 0
                    ),
                    "warm_allocated_bytes": warm_physical,
                    "warm_reserved_bytes": pool["reserved_bytes"],
                    "warm_committed_bytes": pool["committed_bytes"],
                    "shared_borrow_reserved_bytes": pool["borrowed_bytes"],
                    "compute_pressure": placement.sample() if placement is not None else None,
                    "tool_call_cpu_placement_changes": topology_changes,
                    "cgroup_cpu": cpu_sampler.sample() if cpu_sampler is not None else {
                        "measurement": "unavailable",
                        "reason": cpu_unavailable_reason,
                    },
                })
            sampler.sample_hook = record_memory_sample
        policy_events = PolicyEventExecutor(workers=policy_event_workers(arm))
        watermark_controller = next(iter(domains.values())).controller
        sandbox_create_gate = Semaphore(self._sandbox_create_limit(arm.concurrency))
        self._runtime_memory = None
        self._priority_domain = None
        if arm.resources.local_memory_cgroup:
            # All supported policies use neutral per-VM leaves.
            self._priority_domain = PriorityDomain(Path(arm.resources.local_memory_cgroup),
                concurrency=arm.concurrency, repetition=arm.repetition,
                legacy=False,
                journal_directory=self.output_root/'priority-domains',
                topology_mode=arm.execution.tool_call_cpu_policy,
                topology_pressure_ratio=arm.execution.tool_call_cpu_pressure_ratio,
                topology_pressure_samples=arm.execution.tool_call_cpu_pressure_samples,
                topology_group_max_active=arm.execution.tool_call_cpu_group_max_active,
                topology_min_duration_seconds=arm.execution.tool_call_cpu_min_duration_seconds,
                topology_include_smt=arm.execution.tool_call_cpu_include_smt,
                emit=events.write,
                placements={i: (session_domain(i).cpus,
                                session_domain(i).arm.resources.local_numa_node)
                            for i in range(arm.concurrency)}
                    if arm.execution.placement_policy not in {'os_default', 'load_aware'}
                    and not arm.policy.template_aware_placement else {})
            try:
                self._priority_domain.start()
                self._runtime_memory = PressureControl(
                    Path(arm.resources.local_memory_cgroup), mode=arm.policy.runtime_control,
                    options=arm.resources.pressure_control,
                    memory_protection=arm.policy.memory_protection,
                    cpu_release_boost=arm.policy.cpu_release_boost,
                    interval=arm.resources.runtime_poll_interval_seconds, emit=events.write)
                self._runtime_memory.start()
                demand_ledger.check = self._runtime_memory.check
            except BaseException:
                try:
                    if self._runtime_memory is not None:
                        self._runtime_memory.close()
                finally:
                    self._priority_domain.close()
                    policy_events.close()
                raise
        sampler.start()
        for domain in domains.values():
            if domain.controller is not None:
                domain.controller.start()
        sessions: list[dict[str, Any]] = []
        session_failures: list[dict[str, Any]] = []
        failure: Exception | None = None
        infrastructure_error: Exception | None = None
        interrupted = False
        arm_started_wall = time.time()
        arm_started_monotonic = time.monotonic()
        pool = ThreadPoolExecutor(
            max_workers=arm.concurrency, thread_name_prefix="agent"
        )
        futures: dict[Any, int] = {}
        def check_shared_health():
            try:
                if self._runtime_memory is not None:
                    self._runtime_memory.check()
                for domain in domains.values():
                    if domain.controller is not None and domain.controller._error is not None:
                        raise domain.controller._error
                    if domain.coordinator._cache_reclaim_error is not None:
                        raise domain.coordinator._cache_reclaim_error
            except Exception as exc:
                raise ArmInfrastructureError("shared policy controller failed") from exc

        def run_placed_session(index):
            offset = (index*arm.execution.stagger_interval_seconds
                      if arm.execution.arrival_schedule.value == 'fixed_stagger' else 0)
            time.sleep(max(0., arm_started_monotonic+offset-time.monotonic()))
            with creation_slot(sandbox_create_gate, events, f'{arm.arm_id}-{index:04d}') as slot:
                return run_placed_session_impl(index, slot)

        def run_placed_session_impl(index, slot):
            session_id = f"{arm.arm_id}-{index:04d}"
            domain = session_domain(index)
            session_case = session_case_for(arm, index)
            tool_sandbox = session_case.sandbox or arm.sandbox
            pair_mib = arm.resources.lifecycle_reservation_mib(
                arm.runtime.memory_mib + tool_sandbox.memory_mib)
            started = time.monotonic()
            if placement is not None:
                offset = (index*arm.execution.stagger_interval_seconds
                          if arm.execution.arrival_schedule.value == 'fixed_stagger' else 0)
                time.sleep(max(0., arm_started_monotonic+offset-time.monotonic()))
                started = time.monotonic()
                fixed = (None if arm.execution.placement_policy == 'load_aware'
                         else
                         'local' if os_default else
                         place_session(arm.resources.compute_nodes, index,
                                       arm.execution.session_compute_nodes).node_id)
                key = placement.acquire(
                    session_id, vcpus=arm.runtime.vcpu+tool_sandbox.vcpu,
                    memory_mib=pair_mib, timeout=arm.execution.arm_timeout_seconds,
                    fixed=fixed, template_id=tool_sandbox.template,
                    template_aware=arm.policy.template_aware_placement,
                    template_sharing_supported=(
                        arm.resources.template_base_sharing == 'required'
                    ),
                    balance_slack=arm.execution.template_balance_slack,
                )
                domain = domains[key]
                if not os_default:
                    self._priority_domain.placements[index] = (domain.cpus, domain.arm.resources.local_numa_node)
                    self._priority_domain.configure_topology_placement(
                        index, domain.cpus, domain.arm.resources.local_numa_node,
                    )
            waited = time.monotonic()-started
            try:
                result = self._run_session(domain.arm, index, domain.coordinator, events,
                    policy_events, prediction_providers.get(session_case.case_id), None,
                    domain.sampler.observe, arm_started_wall, arm_started_monotonic,
                    snapshot_pool, domain.borrower, domain.controller,
                    creation_reserved=placement is not None, creation_gate_info=slot)
                result['placement_wait_seconds'] = waited if placement is not None else 0.
                offered = arm_started_wall + (index*arm.execution.stagger_interval_seconds
                    if arm.execution.arrival_schedule.value == 'fixed_stagger' else 0)
                result['offered_to_completion_seconds'] = max(0.,
                    result['timeline']['final_agent_completion']-offered)
                result['compute_node'] = (None if os_default else
                    key if placement is not None else result.get('compute_node'))
                return result
            finally:
                if placement is not None:
                    try:
                        with demand_ledger.condition:
                            if (session_id, 'create') in demand_ledger.claims:
                                domain.coordinator.release(session_id, pair_mib, wait_class='create')
                        placement.release(session_id)
                    except Exception as exc:
                        raise ArmInfrastructureError(
                            f"placement cleanup incomplete for {session_id}") from exc
        try:
            placement = (SessionPlacement(domains,
                cpu_overcommit=arm.execution.placement_cpu_overcommit,
                monitor=CpuMonitor({k:d.cpus for k,d in domains.items()},
                                   interval=arm.execution.memory_sample_interval_seconds),
                emit=events.write) if arm.resources.compute_nodes else None)
            futures = {
                pool.submit(run_placed_session, index): index
                for index in range(arm.concurrency)
            }
            failure = collect_session_results(
                futures, timeout=arm.execution.arm_timeout_seconds,
                sessions=sessions, failures=session_failures, events=events,
                arm_id=arm.arm_id, check_health=check_shared_health,
            )
            pool.shutdown(wait=True)
        except (KeyboardInterrupt, SystemExit):
            # ThreadPoolExecutor's context manager waits for every running
            # session before unwinding. On SIGTERM/KeyboardInterrupt that can
            # strand the CLI behind blocked VM calls and prevent lab cleanup.
            # Destroy this run's VMs first so those calls unblock, and cancel
            # sessions that have not started.
            for future in futures:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            try:
                self.client.kill_owned_sandboxes(self.task_uid)
            except Exception:
                pass
            interrupted = True
            raise
        except Exception as exc:
            interrupted = True
            if isinstance(exc, ArmInfrastructureError):
                infrastructure_error = exc
            for future in futures:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            try:
                self.client.kill_owned_sandboxes(self.task_uid)
            except Exception:
                pass
            failure = exc
            events.write({"event": "arm_failed", "error": str(exc), "type": type(exc).__name__})
        finally:
            policy_events.close(wait=not interrupted)
            # Watermark threads call DemandLedger.check(), which delegates to
            # the shared pressure controller. Stop those readers before
            # marking the pressure controller stopped.
            for domain in domains.values():
                if domain.controller is not None:
                    try:
                        domain.controller.stop()
                    except Exception as exc:
                        infrastructure_error = infrastructure_error or exc
                        failure = failure or exc
            if self._runtime_memory is not None:
                try:
                    self._runtime_memory.close()
                except Exception as exc:
                    infrastructure_error = infrastructure_error or exc
                    failure = failure or exc
            # Isolation barrier: all session threads have ended, then kill and
            # verify every task-owned sandbox before the next arm can begin.
            cleanup_error = None
            try:
                self.client.kill_owned_sandboxes(self.task_uid)
            except Exception as exc:
                cleanup_error = exc
            if self._priority_domain is not None:
                try:
                    self._priority_domain.close()
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
            active_tool_call_ids = (
                self._priority_domain.active_tool_call_ids()
                if self._priority_domain is not None else ()
            )
            if active_tool_call_ids:
                cleanup_error = cleanup_error or RuntimeError(
                    'C2 Tool-call CPU assignments remained after session finalization: '
                    f'{list(active_tool_call_ids)}'
                )
            if cleanup_error is None:
                for domain in domains.values():
                    if domain.borrower is not None:
                        for sandbox_id in domain.borrower.borrowed():
                            domain.borrower.release_destroyed(sandbox_id)
            time.sleep(arm.execution.stabilization_seconds)
            memory = sampler.stop()
            cpu_summary = (
                cpu_sampler.summary() if cpu_sampler is not None else {
                    "measurement": "unavailable",
                    "reason": cpu_unavailable_reason,
                }
            )
            events.write({"event": "memory_sampling", "observation": sampler.observe(),
                          "interval_seconds": sampler.interval_s,
                          "samples": sampler.samples()})
            if cleanup_error is not None:
                infrastructure_error = infrastructure_error or cleanup_error
                failure = failure or cleanup_error
            domain_oom_kills = (self._runtime_memory.stats.get('oom_kill_events', 0)
                                if self._runtime_memory is not None else 0)
            if domain_oom_kills:
                failure = failure or RuntimeError(f'experiment cgroup OOM kills: {domain_oom_kills}')
            if memory.host_oom_kill_events:
                failure = failure or RuntimeError(
                    "host OOM kill observed during arm: "
                    f"{memory.host_oom_kill_events}"
                )
                if memory.host_oom_kill_events > domain_oom_kills:
                    infrastructure_error = infrastructure_error or RuntimeError(
                        'host OOM kills occurred outside the experiment cgroup')
            pool_budget_bytes = arm.resources.pool_memory_budget_mib * 1024 * 1024
            # The admission budget is a policy threshold, not memory.max.
            # Exceeding it is measured in every arm, including admission OFF.
        node_results = {}
        for node_id, domain in domains.items():
            c = domain.controller
            node_results[node_id] = {
                "resources": domain.arm.resources.model_dump(mode="json"),
                "admission_control": domain.coordinator.admission_metrics(),
                "checkpoint_capacity_skips": domain.coordinator.checkpoint_capacity_skips,
                "peak_commitment_bytes": domain.coordinator.peak_commitment_bytes,
                "observed_peak_bytes": domain.peak_bytes,
                "placements": [row for row in self._priority_domain.observations
                               if row['compute_numa'] == domain.arm.resources.local_numa_node]
                              if self._priority_domain else [],
                "watermarks": {name: getattr(c, name) for name in (
                    "low", "high", "hard", "peak_local_bytes", "high_crossings",
                    "high_overshoot_byte_seconds", "high_overshoot_seconds",
                    "hard_overshoot_byte_seconds", "hard_overshoot_seconds", "borrow_count",
                    "return_count", "return_release_count",
                )} if c else None,
            }
        if arm.resources.compute_nodes:
            coordinators = [d.coordinator for d in domains.values()]
            coordinator = SimpleNamespace(**{
                name: sum(getattr(c, name) for c in coordinators) for name in (
                    "pause_count", "pause_service_seconds", "resume_count", "resume_service_seconds", "blocked_seconds",
                )
            }, peak_commitment_bytes=None,
                admission_metrics=lambda: {"scope": "per_compute_node", "nodes": {
                    key: value["admission_control"] for key, value in node_results.items()
                }})
            controllers = [d.controller for d in domains.values() if d.controller is not None]
            watermark_controller = SimpleNamespace(**{
                name: sum(getattr(c, name) for c in controllers) for name in (
                    "low", "high", "hard", "high_crossings", "high_overshoot_byte_seconds",
                    "high_overshoot_seconds", "hard_overshoot_byte_seconds", "hard_overshoot_seconds",
                    "borrow_count", "borrow_service_seconds", "return_count",
                    "return_release_count", "return_service_seconds",
                )
            }, peak_local_bytes=memory.peak_used_delta_bytes, **{
                name: max(getattr(c, name) for c in controllers) for name in (
                    "peak_shared_live_bytes", "peak_combined_live_bytes", "peak_shared_total_bytes",
                )
            }) if controllers else None
        status = RunStatus.SUCCEEDED if failure is None else RunStatus.FAILED
        duration = time.monotonic() - started
        session_durations = [float(item.get("agent_jct_seconds", item["duration_seconds"]))
                             for item in sessions]
        tool_latencies = [float(value) for item in sessions for value in item["tool_latencies"]]
        tool_steps = sum(int(item["tool_steps"]) for item in sessions)
        model_steps = sum(int(item["model_steps"]) for item in sessions)
        tool_execution_observations = [
            dict(record)
            for item in sessions for record in item.get("prediction_records", [])
        ]
        observation_summary = summarize_tool_execution_observations(
            tool_execution_observations
        )
        workload_starts = [float(item["timeline"]["agent_execution_start"])
                           for item in sessions if item.get("timeline", {}).get("agent_execution_start")]
        workload_ends = [float(item["timeline"]["final_agent_completion"])
                         for item in sessions if item.get("timeline", {}).get("final_agent_completion")]
        workload_window = (max(workload_ends) - min(workload_starts)
                           if workload_starts and workload_ends else None)
        provenance = self._provenance()
        provenance.update(self._arm_provenance(arm))
        if prediction_provider is not None:
            prediction_artifacts_by_case = {}
            for case in session_cases:
                provider = prediction_providers[case.case_id]
                observations = [
                    record
                    for item in sessions
                    if item.get("case_id") == case.case_id
                    for record in item.get("prediction_records", [])
                    if record.get("execution_scope", "agent-tool") == "agent-tool"
                ]
                prediction_artifacts_by_case[case.case_id] = provider.provenance(
                    observations
                )
            provenance["prediction_artifacts_by_case"] = prediction_artifacts_by_case
            provenance["prediction_artifact"] = (
                next(iter(prediction_artifacts_by_case.values()))
                if len(prediction_artifacts_by_case) == 1 else None
            )
        peak_over_budget_bytes = (
            max(max(0, d.peak_bytes - d.arm.resources.pool_memory_budget_mib*1024**2) for d in domains.values())
            if arm.resources.compute_nodes else max(0, memory.peak_used_delta_bytes - pool_budget_bytes)
        )
        result = ResultEnvelope(
            run_id=self.run_id, attempt_id=self.attempt_id,
            sandbox_task_uid=self.task_uid,
            experiment_id=self.spec.experiment_id, arm=arm,
            provenance=provenance, status=status,
            failure_category=classify_arm_failure(failure, domain_oom_kills=domain_oom_kills,
                                                  infrastructure_error=infrastructure_error,
                                                  session_failure=bool(session_failures) and not interrupted),
            started_at=started_at, completed_at=utcnow(),
            correctness={
                "completed_sessions": sum(item.get("valid", False) for item in sessions),
                "failed_sessions": arm.concurrency - sum(item.get("valid", False) for item in sessions),
                "session_failures": session_failures,
                "arm_interrupted": interrupted,
                "validation_passed": failure is None and all(item.get("valid", False) for item in sessions),
                "native_tool_exact_id_join_rate": (
                    min(
                        float((item.get("native_tool_artifact_validation") or {})
                             .get("exact_id_join_rate", 0.0))
                        for item in sessions
                        if item.get("native_tool_artifact_validation") is not None
                    )
                    if any(item.get("native_tool_artifact_validation") is not None for item in sessions)
                    else None
                ),
                "native_tool_telemetry_loss_total": sum(
                    int((item.get("native_tool_artifact_validation") or {})
                        .get("telemetry_loss_total", 0))
                    for item in sessions
                ),
                "failure": None if failure is None else str(failure),
                "infrastructure_failure": str(infrastructure_error) if infrastructure_error else None,
                "experiment_cgroup_oom_kill_events": domain_oom_kills,
                "session_output_hashes": [item.get("output_hash") for item in sessions],
            },
            performance={
                "observed_tool_concurrency": observed_tool_concurrency(sessions),
                "observed_agent_tool_concurrency": observed_tool_concurrency(sessions, execution_scope='agent-tool'),
                "offered_to_completion_p50_seconds": percentile(
                    [s['offered_to_completion_seconds'] for s in sessions if 'offered_to_completion_seconds' in s], .5),
                "offered_to_completion_p95_seconds": percentile(
                    [s['offered_to_completion_seconds'] for s in sessions if 'offered_to_completion_seconds' in s], .95),
                "offered_to_completion_p99_seconds": percentile(
                    [s['offered_to_completion_seconds'] for s in sessions if 'offered_to_completion_seconds' in s], .99),
                "placement_wait_total_seconds": sum(s.get('placement_wait_seconds', 0.) for s in sessions),
                "duration_seconds": duration,
                "infrastructure_inclusive_duration_seconds": duration,
                "workload_window_seconds": workload_window,
                "agents_per_minute": len(sessions) / max(workload_window or duration, 1e-9) * 60,
                "success_rate": sum(item.get("valid", False) for item in sessions) / arm.concurrency,
                "steps_per_minute": (tool_steps + model_steps) / max(workload_window or duration, 1e-9) * 60,
                "experiment_cpu": cpu_summary,
                "tool_steps": tool_steps, "model_steps": model_steps,
                "jct_mean_seconds": statistics.fmean(session_durations) if session_durations else None,
                "jct_p50_seconds": percentile(session_durations, 0.50),
                "jct_p90_seconds": percentile(session_durations, 0.90),
                "jct_p95_seconds": percentile(session_durations, 0.95),
                "jct_p99_seconds": percentile(session_durations, 0.99),
                "tool_latency_mean_seconds": statistics.fmean(tool_latencies) if tool_latencies else None,
                "tool_latency_p50_seconds": percentile(tool_latencies, 0.50),
                "tool_latency_p90_seconds": percentile(tool_latencies, 0.90),
                "tool_latency_p95_seconds": percentile(tool_latencies, 0.95),
                "tool_latency_p99_seconds": percentile(tool_latencies, 0.99),
                "sandbox_create_mean_seconds": statistics.fmean(
                    [float(item["create_seconds"]) for item in sessions]) if sessions else None,
                "pause_count": coordinator.pause_count,
                "pause_service_seconds": coordinator.pause_service_seconds,
                "resume_count": coordinator.resume_count,
                "resume_service_seconds": coordinator.resume_service_seconds,
                "local_high_crossings": (
                    watermark_controller.high_crossings
                    if watermark_controller is not None else None
                ),
                "local_high_overshoot_byte_seconds": (
                    watermark_controller.high_overshoot_byte_seconds
                    if watermark_controller is not None else None
                ),
                "local_high_overshoot_seconds": (
                    watermark_controller.high_overshoot_seconds
                    if watermark_controller is not None else None
                ),
                "local_hard_overshoot_byte_seconds": (
                    watermark_controller.hard_overshoot_byte_seconds
                    if watermark_controller is not None else None
                ),
                "local_hard_overshoot_seconds": (
                    watermark_controller.hard_overshoot_seconds
                    if watermark_controller is not None else None
                ),
                "shared_memory_borrow_count": (
                    watermark_controller.borrow_count
                    if watermark_controller is not None else None
                ),
                "shared_memory_borrow_service_seconds": (
                    watermark_controller.borrow_service_seconds
                    if watermark_controller is not None else None
                ),
                "shared_memory_return_count": (
                    watermark_controller.return_count
                    if watermark_controller is not None else None
                ),
                "shared_memory_return_release_count": (
                    watermark_controller.return_release_count
                    if watermark_controller is not None else None
                ),
                "shared_memory_return_service_seconds": (
                    watermark_controller.return_service_seconds
                    if watermark_controller is not None else None
                ),
                "blocked_admission_seconds": sum(d.coordinator.blocked_seconds for d in domains.values()),
                "admission_control": coordinator.admission_metrics(),
                "runtime_memory_control": ({**self._runtime_memory.provenance,
                                            **self._runtime_memory.stats,
                                            "priority_cpu_weights": self._priority_domain.cpu_weights}
                                           if self._runtime_memory else None),
                "priority_sessions": {
                    priority: {
                        "offered": sum(p == priority for p in self._priority_domain.priorities.values()),
                        "completed": sum(s.get('valid', False) and s.get('priority') == priority for s in sessions),
                        "durations_seconds": [s['agent_jct_seconds'] for s in sessions
                            if s.get('valid', False) and s.get('priority') == priority],
                    } for priority in ('high', 'low')
                } if self._priority_domain else {},
                "compute_nodes": node_results,
                "placement_policy": arm.execution.placement_policy,
                "template_aware_placement": arm.policy.template_aware_placement,
                "template_affinity_decisions": (
                    placement.template_affinity_decisions if placement else 0
                ),
                "template_cache_hits": placement.template_cache_hits if placement else 0,
                "tool_call_cpu_policy": (
                    self._priority_domain.topology_summary() if self._priority_domain else None
                ),
                "snapshot_aware_residency": arm.policy.snapshot_aware_residency,
                "snapshot_pool": snapshot_pool.snapshot(),
                "tool_execution_observations": tool_execution_observations,
                **observation_summary,
                "session_trace_assignment": [
                    {
                        "session_id": item.get("session_id"),
                        "case_id": item.get("case_id"),
                        "rollout_id": item.get("rollout_id"),
                        "tool_template": item.get("tool_template"),
                        "tool_workspace": item.get("tool_workspace"),
                        "trace_reference": item.get("trace_reference"),
                        "compute_node": item.get("compute_node"),
                        "priority": item.get("priority"),
                    }
                    for item in sorted(sessions, key=lambda value: value["session_id"])
                ],
                "session_timelines": [item.get("timeline") for item in sessions],
                "session_time_spans": [
                    item.get("timeline", {}).get("time_spans", []) for item in sessions
                ],
            },
            memory={
                **asdict(memory),
                # No supported host uncore PMU currently converts remote NUMA
                # transactions to bytes.  Keep this explicitly unavailable;
                # kernel page-allocation events above are a separate proxy.
                "cross_numa_traffic_bytes": None,
                "cross_numa_traffic_measurement": {
                    "status": "unavailable",
                    "reason": (
                        "no qualified platform-specific host uncore PMU event "
                        "maps cross-NUMA transactions to bytes"
                    ),
                    "allocation_proxy_unit": "pages",
                    "allocation_proxy_field": "numa_allocation_event_deltas",
                },
                "admission_high_watermark_bytes": pool_budget_bytes,
                "watermark_scope": "independent_compute_nodes" if arm.resources.compute_nodes else "local",
                "overshoot_seconds_scope": "sum_of_node_seconds" if arm.resources.compute_nodes else "wall_seconds",
                "local_high_watermark_exceeded": peak_over_budget_bytes > 0,
                "peak_local_high_overshoot_bytes": peak_over_budget_bytes,
                "local_low_watermark_bytes": (
                    watermark_controller.low if watermark_controller is not None else None
                ),
                "local_high_watermark_bytes": (
                    watermark_controller.high if watermark_controller is not None else None
                ),
                "local_hard_limit_bytes": (
                    watermark_controller.hard if watermark_controller is not None else None
                ),
                "shared_memory_borrow_limit_bytes": (
                    (shared_borrow_mib or 0) * 1024 * 1024
                    if watermark_controller is not None else None
                ),
                "memory_action_cost_model": (
                    arm.resources.memory_action_costs.model_dump(mode="json")
                    if arm.resources.memory_action_costs is not None else None
                ),
                "peak_local_tier_bytes": (
                    watermark_controller.peak_local_bytes
                    if watermark_controller is not None else None
                ),
                "peak_shared_live_bytes": (
                    watermark_controller.peak_shared_live_bytes
                    if watermark_controller is not None else None
                ),
                "peak_combined_live_bytes": (
                    watermark_controller.peak_combined_live_bytes
                    if watermark_controller is not None else None
                ),
                "peak_shared_total_bytes": (
                    watermark_controller.peak_shared_total_bytes
                    if watermark_controller is not None else None
                ),
                "peak_commitment_bytes": coordinator.peak_commitment_bytes,
            },
            artifacts={
                "events": str(events.path),
                **{
                    f"native_tool_artifacts:{item['session_id']}": str(item["native_tool_artifact_root"])
                    for item in sessions
                    if item.get("native_tool_artifact_root")
                },
                **{
                    f"policy_control:{item['session_id']}": str(item["policy_control_path"])
                    for item in sessions
                    if item.get("policy_control_path")
                },
                **{
                    f"runtime_traces:{item['session_id']}": str(item["runtime_trace_root"])
                    for item in sessions
                    if item.get("runtime_trace_root")
                },
            },
        )
        # Result first, complete marker second. A crash between them retries the
        # entire arm, never treating a partial result as complete.
        atomic_json(result_path, result.model_dump(mode="json"))
        if result.status is RunStatus.SUCCEEDED:
            atomic_marker(marker_path, arm.spec_digest)
        else:
            marker_path.unlink(missing_ok=True)
        return result

    def _run_session(self, arm: ExperimentArm, index: int, coordinator: PolicyCoordinator,
                     events: EventWriter, policy_events: PolicyEventExecutor,
                     prediction_provider: P50PredictionProvider | None = None,
                     sandbox_create_gate: Semaphore | None = None,
                     physical_observation: Any = None,
                     arm_started_wall: float | None = None,
                     arm_started_monotonic: float | None = None,
                     snapshot_pool: WarmSnapshotPool | None = None,
                     numa_borrower: SandboxNumaBorrower | None = None,
                     watermark_controller: WatermarkController | None = None,
                     creation_reserved: bool = False,
                     creation_gate_info=None) -> dict[str, Any]:
        session_case = session_case_for(arm, index)
        tool_sandbox = session_case.sandbox or arm.sandbox
        if session_case != arm.case or tool_sandbox != arm.sandbox:
            arm = arm.model_copy(update={"case": session_case, "sandbox": tool_sandbox})
        offered_offset = (
            index * arm.execution.stagger_interval_seconds
            if arm.execution.arrival_schedule.value == "fixed_stagger" else 0.0
        )
        if arm_started_monotonic is not None:
            time.sleep(max(0.0, arm_started_monotonic + offered_offset - time.monotonic()))
        session_id = f"{arm.arm_id}-{index:04d}"
        priority_domain = getattr(self, '_priority_domain', None)
        progress_control = (self._runtime_memory
                            if isinstance(self._runtime_memory, PressureControl) else None)
        if progress_control and priority_domain:
            for role in ('runtime', 'tool'):
                progress_control.register(session_id, role, priority_domain.leaves[index, role],
                                          arm.resources.local_numa_node, coordinator)
        def progress_event(kind, identifier):
            if progress_control:
                progress_control.event(session_id, kind, identifier)
        def priority_options(role):
            if priority_domain is None:
                return {}
            def verify(sandbox_id):
                evidence = priority_domain.verify(index, role, sandbox_id)
                if progress_control:
                    progress_control.generation(session_id, role, sandbox_id)
                events.write({'event': 'runtime_memory_placement', 'session_id': session_id, **evidence})
                return evidence
            def prepare():
                if progress_control:
                    progress_control.transition(session_id, role)
                priority_domain.prepare(index, role)
            return {'cgroup_token': priority_domain.token(index, role), 'verify_cgroup': verify,
                    'prepare_cgroup': prepare}
        session_started = time.monotonic()
        session_started_wall = time.time()
        timeline: dict[str, Any] = {
            "session_started": session_started_wall,
            "offered_start_offset_seconds": offered_offset,
            "offered_start_unix_s": (
                arm_started_wall + offered_offset
                if arm_started_wall is not None else session_started_wall
            ),
        }
        runtime_ownership = Ownership(
            self.run_id, self.attempt_id, self.task_uid, self.spec.experiment_id,
            f"{session_id}-runtime", arm.policy.name,
        )
        tool_ownership = Ownership(
            self.run_id, self.attempt_id, self.task_uid, self.spec.experiment_id,
            f"{session_id}-tool", arm.policy.name,
        )
        inference_configuration, credential = resolve_llm_configuration(
            arm.inference.configuration, live=arm.inference.backend.value == "api",
        )
        runtime_env: dict[str, str] = {}
        runtime_allow_out: list[str] = []
        tool_allow_out: list[str] = []
        route_epoch = 0
        ssh_config: NativeSSHConfig | None = None
        if arm.agent.driver is AgentDriver.OPENCLAW:
            runtime_env.update({
                # Runtime talks only to the Worker-owned node-routed gateway;
                # the upstream API key stays in the Worker process.
                "OPENAI_BASE_URL": "",
                "OPENCLAW_MODEL": str(inference_configuration.get("model") or ""),
                "CLAWBOX_RUN_ID": self.run_id,
                "CLAWBOX_ATTEMPT_ID": self.attempt_id,
                "CLAWBOX_TENANT_ID": os.environ.get("CLAWBOX_TENANT_ID", "default"),
                "CLAWBOX_REPO_KEY": (
                    arm.case.repository
                    or str(inference_configuration.get("repo_fingerprint") or arm.case.case_id)
                ),
            })
            for optional in ("CLAWBOX_KB_ENDPOINT", "CLAWBOX_KB_TOKEN"):
                if value := os.environ.get(optional):
                    runtime_env[optional] = value
            control_host = os.environ.get("CLAWBOX_CONTROL_HOST", "").strip()
            try:
                control_address = ipaddress.ip_address(control_host)
            except ValueError as exc:
                raise ValueError(
                    "CLAWBOX_CONTROL_HOST must be a host IP reachable from Runtime VMs"
                ) from exc
            runtime_allow_out.append(
                f"{control_address}/{32 if control_address.version == 4 else 128}"
            )
            # Toolbridge sends authenticated execution START/END directly to
            # this host. Closed-network Tools need the same narrow control
            # route as Runtime; otherwise valid commands fail with exit 125.
            tool_allow_out.append(runtime_allow_out[-1])
            # ModelGateway and PolicyControl share the host address and have
            # distinct direct listeners; Kubernetes/NodePort is not involved.
            if self.model_gateway is None:
                raise RuntimeError("managed ModelGateway is not active")
            if kb_endpoint := runtime_env.get("CLAWBOX_KB_ENDPOINT"):
                runtime_allow_out.append(_network_target(
                    kb_endpoint, label="CLAWBOX_KB_ENDPOINT",
                ))
            runtime_allow_out = list(dict.fromkeys(runtime_allow_out))
        runtime_lifecycle = CubeSandboxLifecycle(
            self.client, template=arm.runtime.template,
            node_name=os.environ.get("CLAWBOX_WORKER_NODE", arm.resources.target_node),
            ownership=runtime_ownership,
            allow_internet_access=arm.runtime.allow_internet_access,
            env_vars=runtime_env,
            network_allow_out=runtime_allow_out,
            network_deny_out=_runtime_network_deny_out(
                arm.runtime.allow_internet_access, runtime_allow_out,
            ),
            physical_observation=physical_observation,
            role="runtime",
            warm_snapshot_root=arm.resources.warm_snapshot_root,
            snapshot_mechanism=arm.resources.snapshot_mechanism,
            snapshot_storage=arm.resources.snapshot_storage,
            lazy_restore=arm.resources.lazy_restore,
            cold_snapshot_root=arm.resources.cold_snapshot_root,
            snapshot_pool=snapshot_pool,
            default_snapshot_tier=arm.resources.default_snapshot_tier,
            snapshot_reservation_bytes=(arm.runtime.memory_mib + 256) * 1024 * 1024,
            numa_borrower=numa_borrower,
            configured_memory_bytes=arm.runtime.memory_mib * 1024 * 1024,
            snapshot_reuse_probability=arm.case.snapshot_reuse_probability,
            warm_restore_seconds_per_gib=(
                arm.resources.snapshot_residency_costs.warm_restore_seconds_per_gib
                if arm.resources.snapshot_residency_costs else None
            ),
            cold_restore_seconds_per_gib=(
                arm.resources.snapshot_residency_costs.cold_restore_seconds_per_gib
                if arm.resources.snapshot_residency_costs else None
            ),
            **priority_options('runtime'),
        )
        ssh_credentials = generate_ssh_credentials()
        tool_env = {
            "TOOL_BRIDGE_LOG_PATH": "/var/lib/clawtune/artifacts/tool-bridge.jsonl",
            "TOOL_BRIDGE_WORKDIR": arm.sandbox.workspace,
            "TOOL_MAX_CONCURRENCY": "1",
            # Each experiment Tool VM runs one OpenClaw execution at a time.
            # The guest reports time_running/time_enabled instead of hiding
            # visible PMU multiplexing. Host VM admission remains the separate
            # system-wide concurrency bound.
            "CLAWTUNE_PMU_ENABLED": "true",
            "CLAWTUNE_PMU_MAX_ACTIVE": "1",
            "CLAWBOX_TOOL_HOST_KEY_B64": base64.b64encode(
                ssh_credentials.host_private.encode()
            ).decode(),
            "CLAWBOX_TOOL_AUTHORIZED_KEY_B64": base64.b64encode(
                (ssh_credentials.client_public + "\n").encode()
            ).decode(),
            "TASK_ID": session_id,
            "CELL_ID": self.task_uid,
            "CLAWBOX_REPOSITORY": arm.case.repository or arm.case.case_id,
        }
        lifecycle = CubeSandboxLifecycle(
            self.client, template=arm.sandbox.template,
            node_name=os.environ.get("CLAWBOX_WORKER_NODE", arm.resources.target_node),
            ownership=tool_ownership, allow_internet_access=arm.sandbox.allow_internet_access,
            env_vars=tool_env,
            network_allow_out=tool_allow_out,
            network_deny_out=_runtime_network_deny_out(
                arm.sandbox.allow_internet_access, tool_allow_out),
            physical_observation=physical_observation,
            role="tool",
            warm_snapshot_root=arm.resources.warm_snapshot_root,
            snapshot_mechanism=arm.resources.snapshot_mechanism,
            snapshot_storage=arm.resources.snapshot_storage,
            lazy_restore=arm.resources.lazy_restore,
            cold_snapshot_root=arm.resources.cold_snapshot_root,
            snapshot_pool=snapshot_pool,
            default_snapshot_tier=arm.resources.default_snapshot_tier,
            snapshot_reservation_bytes=(arm.sandbox.memory_mib + 256) * 1024 * 1024,
            numa_borrower=numa_borrower,
            configured_memory_bytes=arm.sandbox.memory_mib * 1024 * 1024,
            snapshot_reuse_probability=arm.case.snapshot_reuse_probability,
            warm_restore_seconds_per_gib=(
                arm.resources.snapshot_residency_costs.warm_restore_seconds_per_gib
                if arm.resources.snapshot_residency_costs else None
            ),
            cold_restore_seconds_per_gib=(
                arm.resources.snapshot_residency_costs.cold_restore_seconds_per_gib
                if arm.resources.snapshot_residency_costs else None
            ),
            **priority_options('tool'),
        )
        if watermark_controller is not None:
            watermark_controller.register(session_id, lifecycle, runtime_lifecycle)
        lifetime = 0
        gateway_session: SessionGatewayState | None = None
        policy_session = None
        policy_drained = True
        lifetime_acquired = False
        wait_lock = Lock()
        wait_state: dict[str, Any] = {}
        completed_model_requests: set[str] = set()
        generated_model_requests: set[str] = set()
        generated_model_requests_lock = Lock()
        policy_event_errors: list[str] = []
        pending_policy_events: list[Any] = []
        pending_policy_events_lock = Lock()
        finalization_active = False

        def submit_policy_event(operation: Any, *args: Any) -> None:
            future = policy_events.submit(operation, *args)
            with pending_policy_events_lock:
                pending_policy_events.append(future)

        def drain_policy_events() -> None:
            """Finish this session's queued lifecycle work before validation."""
            drain_policy_futures(pending_policy_events, pending_policy_events_lock,
                                 arm.execution.arm_timeout_seconds)

        if arm.agent.driver is AgentDriver.OPENCLAW:
            if self.model_gateway is None:
                raise RuntimeError("managed ModelGateway is not active")
            # Native OpenClaw can keep its long-lived Agent process in a VM
            # snapshot.  During a model wait, snapshot both VMs for a
            # snapshot_pause policy; Runtime is restored before the gateway
            # response is released, while Tool may stay swapped until the
            # next SSH admission.
            runtime_snapshot_enabled = (
                arm.policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE
                and arm.policy.runtime_snapshot
            )
            def pause_for_model_wait(event: dict[str, Any]) -> None:
                try:
                    # Recheck live pressure; the queued event may be stale.
                    # Candidate selection/claim shares the Tool admission lock.
                    if coordinator.blocked_demand_pressure():
                        coordinator.relieve_pressure()
                except Exception as exc:
                    policy_event_errors.append(f"pause: {type(exc).__name__}: {exc}")
                    events.write({"event": "policy_event_failed", "session_id": session_id,
                                  "operation": "pause", "error": str(exc),
                                  "error_type": type(exc).__name__})

            def before_runtime_idle_pause() -> None:
                wait_state["runtime_agent_pid_before_pause"] = observe_openclaw_agent_pid(
                    "before_runtime_pause")
                gateway_session.invalidate_pending_delivery()

            def restore_runtime_for_model_wait_locked(
                event: dict[str, Any], *, phase: str,
            ) -> None:
                """Restore Runtime while the caller holds ``wait_lock``."""
                if not runtime_snapshot_enabled:
                    return
                if runtime_lifecycle.resident:
                    return
                started = time.time()
                agent_pid_before = wait_state.get(
                    "runtime_agent_pid_before_pause"
                )
                self._restore_with_one_victim(
                    arm, session_id, runtime_lifecycle, coordinator, events,
                    role="runtime",
                )
                relay_started = time.time()
                relay_result = _execute_idempotent(
                    runtime_executor,
                    f"curl -fsS -X POST {shlex.quote(RELAY_CHECKPOINT_URL)}",
                    10,
                )
                if relay_result.exit_code:
                    raise RuntimeError(
                        "Runtime model relay checkpoint notification failed: "
                        f"{relay_result.stderr[-1000:]}"
                    )
                relay_finished = time.time()
                _record_time_span(
                    timeline, "model.relay_reconnect",
                    relay_started, relay_finished,
                    role="runtime", operation="model_relay_reconnect",
                    execution_id=str(event.get("request_id") or ""),
                )
                events.write({
                    "event": "model_relay_reconnected",
                    "session_id": session_id,
                    "request_id": event.get("request_id"),
                    "service_seconds": max(0.0, relay_finished - relay_started),
                })
                agent_pid_after = observe_openclaw_agent_pid(
                    f"after_runtime_restore_{phase}"
                )
                if agent_pid_before is not None and agent_pid_before != agent_pid_after:
                    raise RuntimeError(
                        "OpenClaw agent PID changed across Runtime pause/restore: "
                        f"{agent_pid_before} -> {agent_pid_after}"
                    )
                finished = time.time()
                wait_state.update({
                    "runtime_restore_started_at": started,
                    "runtime_restore_completed_at": finished,
                    "runtime_restore_request_id": event.get("request_id"),
                    "runtime_agent_pid_after_restore": agent_pid_after,
                })

            def on_model_request_started(event: dict[str, Any]) -> None:
                """Register actual waiting synchronously; enqueue only blocking policy work."""
                request_id = str(event.get("request_id"))
                progress_event('model_start', request_id)
                with wait_lock, generated_model_requests_lock:
                    if (request_id in generated_model_requests
                            or request_id in completed_model_requests):
                        return
                    wait_state.clear()
                    wait_state.update(request_id=request_id,
                                      request_started_at=event.get("request_started_at"))
                    if arm.policy.eviction is EvictionPolicy.WAIT_AWARE_PRESSURE:
                        coordinator.begin_model_wait(session_id, request_id)
                if arm.policy.reclamation is not ReclamationPolicy.RESIDENT:
                    submit_policy_event(pause_for_model_wait, event)

            def before_model_response_ready(step: int | None, message: dict[str, Any],
                                            event: dict[str, Any]) -> dict[str, Any]:
                request_id = str(event.get("request_id"))
                progress_event('model_end', request_id)
                # Publish generation before waiting for lifecycle serialization.
                # A queued eager-pause callback must not start checkpointing a
                # Runtime after its model response already exists.
                with generated_model_requests_lock:
                    generated_model_requests.add(request_id)
                coordinator.complete_model_wait(session_id, request_id)
                # A paused Runtime cannot receive the pending HTTP response.
                # Restore it synchronously in the gateway producer before the
                # response becomes visible to OpenClaw. Tool restore remains
                # admission-scoped and happens on the next SSH invocation.
                with wait_lock:
                    # Claim completion before checking residency. Otherwise a
                    # queued eager pause can run between a resident check and
                    # this marker, snapshotting Runtime after the response is
                    # ready and leaving no callback able to restore it.
                    completed_model_requests.add(request_id)
                    for vm, prefix in ((lifecycle, ""), (runtime_lifecycle, "runtime_")):
                        timing = next((t for t in reversed(vm.timings)
                                       if t.get("operation") == "checkpoint"
                                       and t.get("status", "ok") != "error"
                                       and t.get("started_unix_s", 0) >= float(event["request_started_at"])), None)
                        if timing is not None:
                            wait_state.update({
                                prefix + "pause_request_id": request_id,
                                prefix + "pause_started_at": timing["started_unix_s"],
                                prefix + "pause_completed_at": timing["completed_unix_s"],
                            })
                    restore_runtime_for_model_wait_locked(
                        event, phase="response",
                    )
                    started = float(event["request_started_at"])
                    generated = float(event["model_generated_at"])
                    actual_wait = max(0.0, generated - started)
                    request_state = (
                        dict(wait_state)
                        if str(wait_state.get("request_id")) == request_id
                        else {}
                    )
                    if request_state:
                        wait_state["model_generated_at"] = generated
                    admission = {
                        "request_id": event.get("request_id"),
                        "model_step": step,
                        "runtime_snapshot_enabled": runtime_snapshot_enabled,
                        "tool_snapshot_performed": (
                            str(request_state.get("pause_request_id")) == request_id
                        ),
                        "runtime_snapshot_performed": (
                            str(request_state.get("runtime_pause_request_id")) == request_id
                        ),
                        "actual_wait_seconds": actual_wait,
                        "tool_pause_started_at": request_state.get("pause_started_at"),
                        "tool_pause_completed_at": request_state.get("pause_completed_at"),
                        "restore_started_at": request_state.get("restore_started_at"),
                        "restore_completed_at": request_state.get("restore_completed_at"),
                        "runtime_pause_started_at": request_state.get(
                            "runtime_pause_started_at"
                        ),
                        "runtime_pause_completed_at": request_state.get(
                            "runtime_pause_completed_at"
                        ),
                        "runtime_restore_started_at": request_state.get(
                            "runtime_restore_started_at"
                        ),
                        "runtime_restore_completed_at": request_state.get(
                            "runtime_restore_completed_at"
                        ),
                        "runtime_agent_pid_before_pause": request_state.get(
                            "runtime_agent_pid_before_pause"
                        ),
                        "runtime_agent_pid_after_restore": request_state.get(
                            "runtime_agent_pid_after_restore"
                        ),
                        "tool_call_count": len(message.get("tool_calls") or []),
                    }
                    return admission

            trace_path = (
                Path(arm.case.replay_trace_reference)
                if arm.inference.backend.value == "replay"
                and arm.case.replay_trace_reference else None
            )
            gateway_session = self.model_gateway.register(
                session_id=session_id,
                store_path=self.output_root / "model-gateway" / f"{session_id}.json",
                mode=arm.inference.backend.value,
                trace=trace_path,
                time_scale=float(inference_configuration.get("time_scale", 1.0)),
                max_model_steps=inference_configuration.get("max_model_steps"),
                # The supervisor's arm deadline is the authoritative limit.
                # A model response may be intentionally held for admission
                # longer than the gateway's historical 600-second default.
                request_timeout_s=max(
                    120, arm.execution.arm_timeout_seconds + 60,
                ),
                tool_transport_timeout_s=max(
                    120, arm.execution.arm_timeout_seconds + 60,
                ),
                upstream_base_url=str(inference_configuration.get("base_url") or "") or None,
                upstream_api_key=credential or None,
                upstream_model=str(inference_configuration.get("model") or "") or None,
                on_request_started=on_model_request_started,
                before_response_ready=before_model_response_ready,
            )
            runtime_env["CLAWBOX_MODEL_GATEWAY_TOKEN"] = gateway_session.token
            # CubeSandboxLifecycle snapshots env_vars at construction time.
            # The per-session gateway token is created later so callbacks can
            # capture both lifecycles; copy it into the lifecycle payload
            # before the Runtime VM is materialized.
            runtime_lifecycle.env_vars["CLAWBOX_MODEL_GATEWAY_TOKEN"] = (
                gateway_session.token
            )
        # Delay coordinator registration until every pre-VM validation and
        # per-session gateway registration has succeeded.  Otherwise a bad
        # replay path or gateway setup error can leave a non-existent session
        # occupying policy state even though the main cleanup block was never
        # entered.
        coordinator.register(session_id, lifecycle)
        if self._priority_domain is not None:
            tool_leaf = self._priority_domain.leaves[index, 'tool']
            coordinator.register_memory(session_id,
                tool=lambda: read_priority_memory_charge(
                    tool_leaf, arm.resources.local_numa_node),
                lifecycle=lambda: tuple(sum(values) for values in zip(
                    read_priority_memory_charge(tool_leaf, arm.resources.local_numa_node),
                    read_priority_memory_charge(self._priority_domain.leaves[index, 'runtime'],
                                                arm.resources.local_numa_node))))

        coordinator.register_runtime(
            session_id, runtime_lifecycle,
            before_pause=(
                before_runtime_idle_pause
                if gateway_session is not None else None
            ),
        )
        try:
            if creation_gate_info is not None:
                timeline['sandbox_create_gate_wait_start'] = creation_gate_info[0]
                timeline['sandbox_create_gate_acquired'] = creation_gate_info[1]
            if lifetime:
                coordinator.acquire_capacity(
                    session_id, lifetime, arm.execution.arm_timeout_seconds,
                )
                lifetime_acquired = True
            if sandbox_create_gate is not None:
                timeline["sandbox_create_gate_wait_start"] = time.time()
                sandbox_create_gate.acquire()
                timeline["sandbox_create_gate_acquired"] = time.time()
                events.write({
                    "event": "sandbox_create_gate_acquired", "session_id": session_id,
                    "wait_seconds": max(
                        0.0,
                        timeline["sandbox_create_gate_acquired"]
                        - timeline["sandbox_create_gate_wait_start"],
                    ),
                })
            pair_reservation = None
            try:
                if not lifetime:
                    pair_amount = arm.resources.lifecycle_reservation_mib(
                        arm.sandbox.memory_mib + arm.runtime.memory_mib)
                    pair_wait = 0.0 if creation_reserved else coordinator.acquire(
                        session_id, pair_amount, arm.execution.arm_timeout_seconds,
                        wait_class="create",
                    )
                    pair_reservation = pair_amount
                timeline["sandbox_create_start"] = time.time()
                timeline["tool_create_start"] = time.time()
                if lifetime:
                    tool_create_s, tool_create_reservation_wait = lifecycle.start(), 0.0
                else:
                    tool_create_s, tool_create_reservation_wait = lifecycle.start(), pair_wait
                timeline["tool_ready"] = time.time()
                events.write({"event": "sandbox_created", "session_id": session_id,
                              "role": "tool", "service_seconds": tool_create_s,
                              "materialization_reservation_mib": arm.resources.lifecycle_reservation_mib(arm.sandbox.memory_mib),
                              "materialization_reservation_wait_seconds": tool_create_reservation_wait,
                              "lifecycle_timing": lifecycle.timings[-1],
                              "sandbox_id": self.client.sandbox_id(lifecycle.sandbox)})
                setup_route = None
                if arm.agent.driver is AgentDriver.OPENCLAW:
                    tool_endpoint = self.client.get_tcp_endpoint(lifecycle.sandbox, 2222)
                    route_epoch += 1
                    setup_route = native_ssh_route(tool_endpoint, epoch=route_epoch)
                    try:
                        endpoint_address = ipaddress.ip_address(setup_route.host)
                    except ValueError as exc:
                        raise RuntimeError(
                            f"CubeSandbox returned a non-IP TCP endpoint host: {setup_route.host!r}"
                        ) from exc
                    runtime_lifecycle.ensure_network_allow_out(
                        f"{endpoint_address}/{32 if endpoint_address.version == 4 else 128}"
                    )
                timeline["runtime_create_start"] = time.time()
                if lifetime:
                    runtime_create_s, runtime_create_reservation_wait = runtime_lifecycle.start(), 0.0
                else:
                    runtime_create_s, runtime_create_reservation_wait = runtime_lifecycle.start(), 0.0
                timeline["runtime_ready"] = time.time()
                timeline["sandbox_ready"] = timeline["runtime_ready"]
                events.write({"event": "sandbox_created", "session_id": session_id,
                              "role": "runtime", "service_seconds": runtime_create_s,
                              "materialization_reservation_mib": arm.resources.lifecycle_reservation_mib(arm.runtime.memory_mib),
                              "materialization_reservation_wait_seconds": runtime_create_reservation_wait,
                              "lifecycle_timing": runtime_lifecycle.timings[-1],
                              "sandbox_id": self.client.sandbox_id(runtime_lifecycle.sandbox)})
            finally:
                if pair_reservation is not None:
                    # One create claim covers the pair. Its measured growth is
                    # deducted by DemandLedger while either VM is starting.
                    # Release that same claim after success or partial failure;
                    # releasing tool_admission would leave it until unregister.
                    coordinator.release(session_id, pair_reservation, wait_class="create")
                if sandbox_create_gate is not None:
                    sandbox_create_gate.release()
                if creation_gate_info is not None:
                    creation_gate_info[2]()
            create_s = runtime_create_s + tool_create_s
            runtime_executor = CubeCommandExecutor(
                self.client, lambda: runtime_lifecycle.sandbox, cwd=arm.runtime.workspace,
            )
            executor = CubeCommandExecutor(self.client, lambda: lifecycle.sandbox, cwd=arm.sandbox.workspace)
            if arm.sandbox.preflight_command:
                preflight = executor.execute(arm.sandbox.preflight_command, arm.execution.command_timeout_seconds)
                events.write({"event": "task_environment_checked", "session_id": session_id,
                              "exit_code": preflight.exit_code,
                              "stdout": preflight.stdout[-16000:], "stderr": preflight.stderr[-16000:]})
                if preflight.exit_code != 0:
                    raise RuntimeError("Task environment preflight failed: " +
                                       (preflight.stderr or preflight.stdout)[-2000:])
            agent_pid_file = f"/state/openclaw/{session_id}/agent.pid"
            agent_pid_observations: list[dict[str, Any]] = []

            def observe_openclaw_agent_pid(phase: str) -> int | None:
                """Prove the long-lived Runtime agent remains alive across Tool lifecycle work."""
                if arm.agent.driver is not AgentDriver.OPENCLAW:
                    return None
                result = _execute_idempotent(runtime_executor,
                    "pid=$(cat " + shlex.quote(agent_pid_file) + ") && "
                    "case $pid in ''|*[!0-9]*) exit 1;; esac && "
                    "kill -0 $pid && printf '%s' $pid",
                    15,
                )
                if result.exit_code:
                    raise RuntimeError(
                        f"OpenClaw agent PID is not alive during {phase}: {result.stderr[-500:]}"
                    )
                try:
                    pid = int(result.stdout.strip())
                except ValueError as exc:
                    raise RuntimeError(
                        f"OpenClaw agent PID was invalid during {phase}: {result.stdout!r}"
                    ) from exc
                agent_pid_observations.append({"phase": phase, "pid": pid})
                events.write({
                    "event": "openclaw_agent_pid_observed", "session_id": session_id,
                    "phase": phase, "pid": pid, "alive": True,
                })
                return pid

            def resolve_native_ssh_route(phase: str) -> NativeSSHRoute:
                """Resolve CubeSandbox's current route at an operation boundary."""
                nonlocal route_epoch, ssh_config
                if lifecycle.sandbox is None:
                    raise RuntimeError("native SSH route requested before Tool start")
                endpoint = self.client.get_tcp_endpoint(lifecycle.sandbox, 2222)
                route_epoch += 1
                route = native_ssh_route(endpoint, epoch=route_epoch)
                try:
                    endpoint_address = ipaddress.ip_address(route.host)
                except ValueError as exc:
                    raise RuntimeError(
                        f"CubeSandbox returned a non-IP TCP endpoint host: {route.host!r}"
                    ) from exc
                endpoint_cidr = (
                    f"{endpoint_address}/{32 if endpoint_address.version == 4 else 128}"
                )
                added_endpoint_host = runtime_lifecycle.ensure_network_allow_out(endpoint_cidr)
                if ssh_config is not None:
                    # OpenClaw captured its SSH target when run_openclaw was
                    # constructed. This local descriptor is updated only for
                    # the later Worker-owned artifact collection SSH call;
                    # per-admission Agent routing comes from the shim response.
                    ssh_config = replace(ssh_config, target=route.target)
                events.write({
                    "event": "native_ssh_endpoint_resolved", "session_id": session_id,
                    "sandbox_id": route.sandbox_id, "container_port": route.container_port,
                    "endpoint_epoch": route.epoch, "host": route.host, "port": route.port,
                    "tcp_endpoint": endpoint.address, "phase": phase,
                    "runtime_endpoint_host_allowlisted": endpoint_cidr,
                    "runtime_endpoint_host_allowlist_added": added_endpoint_host,
                    "source": "cubesandbox_tcp_endpoint",
                })
                return route

            exit_mismatches = 0
            model_steps = 0
            tool_latencies: list[float] = []
            prediction_records: list[dict[str, Any]] = []
            observed_by_execution: dict[str, Any] = {}
            trace_writer = ClawTuneTraceWriter(
                self.output_root, run_id=self.run_id, session_id=session_id,
                repo_fingerprint=(arm.case.repository or str(
                    inference_configuration.get("repo_fingerprint") or ""
                ) or None),
            )

            def execute_observed(command: str, timeout_s: float,
                                 execution_id: str, *, tool_name: str = "exec",
                                 prediction: dict[str, Any] | None = None,
                                 phase: str = "agent"):
                lifecycle.claim_first_tool_after_restore(execution_id)
                started_wall, started_mono = time.time(), time.monotonic()
                try:
                    observed = executor.execute_observed(
                        command, min(timeout_s, arm.execution.command_timeout_seconds),
                        execution_id=execution_id,
                    )
                except Exception:
                    report = lifecycle.complete_first_tool_after_restore(
                        execution_id, tool_seconds=time.monotonic() - started_mono, status="error")
                    if report is not None:
                        events.write({"event": "first_tool_after_restore", "session_id": session_id, **report})
                    _record_time_span(
                        timeline, "tool.operation", started_wall, time.time(),
                        role="tool", operation=tool_name, execution_id=execution_id,
                        start_monotonic=started_mono, end_monotonic=time.monotonic(),
                        status="error",
                    )
                    raise
                _record_time_span(
                    timeline, "tool.operation", started_wall, time.time(),
                    role="tool", operation=tool_name, execution_id=execution_id,
                    start_monotonic=started_mono, end_monotonic=time.monotonic(),
                    status="ok" if observed.result.exit_code == 0 else "error",
                )
                report = lifecycle.complete_first_tool_after_restore(
                    execution_id, tool_seconds=time.monotonic() - started_mono,
                    status="ok" if observed.result.exit_code == 0 else "error")
                if report is not None:
                    events.write({"event": "first_tool_after_restore", "session_id": session_id, **report})
                observed_by_execution[execution_id] = observed
                trace_writer.record(
                    command, observed.result, execution_id=execution_id,
                    bridge_record=observed.bridge_record, artifacts=observed.artifacts,
                    prediction=prediction, tool_name=tool_name, phase=phase,
                )
                events.write({
                    "event": "clawtune_observation", "session_id": session_id,
                    "execution_id": execution_id, "tool": tool_name,
                    "phase": phase,
                    "prediction": prediction,
                    "telemetry_state": observed.bridge_record.get("telemetry_state"),
                    "telemetry_unavailable_reason": observed.telemetry_unavailable_reason,
                    "artifact_kinds": sorted(observed.artifacts),
                })
                return observed.result

            timeline["agent_execution_start"] = time.time()
            model_trace_path: Path | None = None
            native_artifacts = None
            policy_control_path: Path | None = None
            if arm.agent.driver is AgentDriver.OPENCLAW:
                active_reservations: dict[str, int] = {}
                admitted_routes: dict[str, NativeSSHRoute] = {}
                host_rss_samplers: dict[str, SandboxRSSSampler] = {}
                active_cpu_placements: set[str] = set()
                pending_admissions: set[str] = set()
                reservation_lock = Lock()

                def admit_openclaw_tool(request: dict[str, Any]) -> dict[str, Any]:
                    execution_id = str(request["execution_id"])
                    if progress_control:
                        progress_control.execution_ready(session_id, execution_id,
                            (arm.sandbox.template_id, request['command_sha256']))
                    execution_scope = str(
                        request.get("execution_scope") or "agent-tool"
                    )
                    managed_command = (execution_scope == "agent-tool"
                                       and request.get("operation") != "filesystem")
                    prediction = None
                    if managed_command:
                        if arm.policy.admission is AdmissionPolicy.PREDICTED:
                            if prediction_provider is None:
                                raise PredictionUnavailable(
                                    "predicted command admission requires a prediction provider"
                                )
                            if arm.resources.prediction_mode == "runtime":
                                from .prediction import command_sha256
                                command = request.get("command")
                                if not isinstance(command, str) or command_sha256(command) != request['command_sha256']:
                                    raise PredictionUnavailable("runtime prediction command text/digest mismatch; update the SSH shim")
                                prediction = prediction_provider.resolve(command, request.get('prediction'))
                            else:
                                prediction = prediction_provider.resolve_digest(
                                    str(request['command_sha256']), request.get('prediction'))
                        # Fixed admission needs no prediction object. The
                        # reservation helper reads static_tool_memory_mib.
                    elif arm.policy.admission in {AdmissionPolicy.PREDICTED, AdmissionPolicy.FIXED}:
                        fixed_budget = (arm.resources.non_command_tool_memory_mib
                                        or arm.resources.static_tool_memory_mib)
                        if fixed_budget is None:
                            raise PredictionUnavailable('non-command operations require an explicit fixed host budget')
                        prediction = {
                            'prediction_source': 'non_command_fixed_host_budget',
                            'admission_prediction_target': 'host_cgroup_memory_current_increment',
                            'predicted_incremental_memory_mib': fixed_budget}
                    runtime_memory = getattr(self, '_runtime_memory', None)
                    if runtime_memory is not None:
                        runtime_memory.check()
                    amount = self._tool_reservation_mib(arm, prediction=prediction)
                    local_amount = amount
                    admission_memory_tier = "local"
                    reservation_acquired = False
                    cpu_placement_acquired = False
                    with reservation_lock:
                        pending_admissions.add(execution_id)
                    events.write({'event': 'tool_admission_queued',
                                  'session_id': session_id,
                                  'execution_id': execution_id,
                                  'execution_scope': execution_scope,
                                  'operation': request.get('operation')})
                    with wait_lock:
                        try:
                            # Claim the session before routing memory. Otherwise
                            # checkpoint/restore can reset a just-borrowed VM to
                            # LOCAL while this request carries a zero LOCAL claim.
                            (
                                local_amount,
                                admission_memory_tier,
                                admission_wait,
                                restored_tool,
                            ) = self._prepare_tool_admission(
                                arm, session_id, lifecycle, coordinator, events,
                                watermark_controller, amount,
                                wait_for_admission=lambda operation: (
                                    wait_for_admission_without_lifecycle_lock(
                                        wait_lock, operation,
                                    )
                                ),
                            )
                            reservation_acquired = True
                            if restored_tool:
                                bridge_ready_started = time.monotonic()
                                bridge_error: Exception | None = None
                                bridge_result = None
                                for bridge_attempt in range(1, 31):
                                    try:
                                        # A snapshot restore normally resumes
                                        # the existing bridge process. Probe it
                                        # first; killing a healthy resumed
                                        # bridge creates avoidable races at
                                        # high concurrency. Periodic restarts
                                        # remain an idempotent recovery path.
                                        candidate = executor.execute(
                                            native_tool_bridge_setup_command(
                                                require_progress_events=arm.policy.runtime_control.adaptive,
                                                restart=bridge_attempt in (2, 10, 20),
                                                command_timeout_seconds=(
                                                    arm.execution.command_timeout_seconds
                                                ),
                                            ),
                                            60,
                                        )
                                        if candidate.exit_code == 0:
                                            bridge_result = candidate
                                            break
                                        bridge_error = RuntimeError(
                                            "bridge setup exited "
                                            f"{candidate.exit_code}: {candidate.stderr[-1000:]}"
                                        )
                                    except Exception as exc:
                                        bridge_error = exc
                                    if bridge_attempt < 30:
                                        time.sleep(0.5)
                                if bridge_result is None:
                                    raise RuntimeError(
                                        "Tool telemetry bridge did not recover after restore "
                                        "within 30 readiness attempts; last error: "
                                        f"{type(bridge_error).__name__}: {bridge_error}"
                                    ) from bridge_error
                                events.write({
                                    "event": "tool_restore_ready",
                                    "session_id": session_id,
                                    "execution_id": execution_id,
                                    "attempts": bridge_attempt,
                                    "service_seconds": (
                                        time.monotonic() - bridge_ready_started
                                    ),
                                })
                            # Resolve only after the active mark and memory
                            # reservation. Restore may replace the mapping.
                            route = resolve_native_ssh_route("admit")
                            # Cube may publish a restored mapping slightly
                            # before CubeProxy accepts its fresh route. Prove
                            # reachability and the stable Tool identity from
                            # the Runtime before returning ADMIT; the Agent's
                            # actual SSH subprocess has not started yet.
                            identity_file = f"/state/openclaw/{session_id}/ssh/id_ed25519"
                            known_hosts_file = f"/state/openclaw/{session_id}/ssh/known_hosts"
                            endpoint_host = (
                                f"[{route.host}]" if ":" in route.host else route.host
                            )
                            # The Runtime image's /usr/bin/ssh is the policy
                            # shim while the Agent is active.  An admission
                            # callback must use the per-session preserved
                            # OpenSSH client or it recursively calls /admit
                            # while holding this session's admission lock.
                            real_ssh = openclaw_real_ssh_path(session_id)
                            identity_command = shlex.join([
                                real_ssh, "-i", identity_file,
                                "-o", f"UserKnownHostsFile={known_hosts_file}",
                                "-o", "StrictHostKeyChecking=yes",
                                "-o", "UpdateHostKeys=no",
                                "-o", f"HostKeyAlias={native_ssh_host_key_alias(route.sandbox_id)}",
                                "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
                                "-p", str(route.port), f"executor@{endpoint_host}",
                                "printf %s \"$TASK_ID\"",
                            ])
                            identity_started = time.monotonic()
                            identity_result = None
                            identity_error: Exception | None = None
                            # Under hundreds of simultaneous VM lifecycle operations,
                            # CubeProxy can keep the previous port mapping reachable
                            # beyond the former 15-second window.  The identity check
                            # remains authoritative; allow the already-configured
                            # command timeout (at least 60 seconds here) to absorb
                            # publication lag instead of accepting the wrong Tool.
                            identity_attempt_limit = max(
                                30,
                                min(120, int(arm.execution.command_timeout_seconds * 2)),
                            )
                            for identity_attempt in range(1, identity_attempt_limit + 1):
                                try:
                                    candidate = runtime_executor.execute(identity_command, 30)
                                except Exception as exc:
                                    identity_error = exc
                                    if identity_attempt < identity_attempt_limit:
                                        time.sleep(0.5)
                                    continue
                                if (candidate.exit_code == 0
                                        and candidate.stdout.strip() == session_id):
                                    identity_result = candidate
                                    break
                                if identity_attempt < identity_attempt_limit:
                                    time.sleep(0.5)
                            if identity_result is None:
                                raise RuntimeError(
                                    "native SSH endpoint did not present the intended Tool "
                                    f"identity for {session_id} after {identity_attempt} attempts"
                                ) from identity_error
                            events.write({
                                "event": "native_ssh_identity_ready",
                                "session_id": session_id,
                                "execution_id": execution_id,
                                "sandbox_id": route.sandbox_id,
                                "endpoint_epoch": route.epoch,
                                "attempts": identity_attempt,
                                "service_seconds": time.monotonic() - identity_started,
                            })
                            coordinator.bind_tool_memory(session_id)
                            if (arm.execution.tool_call_cpu_policy == 'c2'
                                    and c2_applies(execution_scope, request.get('operation'))):
                                placement_event = priority_domain.acquire_tool_call(
                                    index, execution_id, vcpus=tool_sandbox.vcpu,
                                )
                                cpu_placement_acquired = bool(
                                    placement_event
                                    and placement_event.get('release_required', True)
                                )
                            host_sampler = SandboxRSSSampler(route.sandbox_id,
                                cgroup=(priority_domain.leaves[index, 'tool'] if priority_domain is not None
                                        else locate_cgroup(route.sandbox_id, Path(arm.resources.local_memory_cgroup))))
                            host_sampler.start()
                            if progress_control:
                                progress_control.execution_admitted(session_id, execution_id,
                                                                    host_sampler.charge_baseline)
                            with reservation_lock:
                                pending_admissions.discard(execution_id)
                                active_reservations[execution_id] = local_amount
                                admitted_routes[execution_id] = route
                                host_rss_samplers[execution_id] = host_sampler
                                if cpu_placement_acquired:
                                    active_cpu_placements.add(execution_id)
                                if execution_scope == "agent-tool":
                                    lifecycle.claim_first_tool_after_restore(execution_id)
                        except Exception as exc:
                            progress_event('complete', execution_id)
                            with reservation_lock:
                                pending_admissions.discard(execution_id)
                            events.write({
                                "event": "tool_admission_failed",
                                "session_id": session_id,
                                "execution_id": execution_id,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "reservation_acquired": reservation_acquired,
                                "cpu_placement_acquired": cpu_placement_acquired,
                            })
                            if cpu_placement_acquired:
                                priority_domain.release_tool_call(execution_id)
                            if reservation_acquired:
                                coordinator.release(session_id, local_amount)
                            with reservation_lock:
                                coordinator.set_tool_active(
                                    session_id,
                                    bool(active_reservations or pending_admissions),
                                )
                            raise
                    prediction_record = dict(prediction or {})
                    prediction_record.setdefault(
                        "canonical_prediction_key",
                        f"sha256:{request['command_sha256']}",
                    )
                    prediction_record.setdefault(
                        "prediction_source", f"policy_{arm.policy.admission.value}"
                    )
                    prediction_record.setdefault("fallback_level", "not_applicable")
                    prediction_record.setdefault(
                        "predicted_incremental_memory_mib",
                        None if arm.policy.admission is AdmissionPolicy.OFF else float(amount)
                    )
                    prediction_record.update({
                                 "session_id": session_id,
                                 "execution_id": execution_id,
                                 "command_sha256": request["command_sha256"],
                                 "operation": request.get("operation"),
                                 "execution_scope": execution_scope,
                                 "actual_measured_memory_mib": None,
                                 "admitted_reservation_mib": amount,
                                 "prediction_reservation_scale": arm.resources.prediction_reservation_scale,
                                 "local_admitted_reservation_mib": local_amount,
                                 "admission_memory_tier": admission_memory_tier,
                                 "admission_blocked_seconds": admission_wait,
                    })
                    prediction_records.append(prediction_record)
                    events.write({
                        "event": "tool_admitted", "session_id": session_id,
                        "execution_id": execution_id,
                        "execution_scope": execution_scope,
                        "operation": request.get("operation"),
                        "command_sha256": request["command_sha256"],
                        "prediction_key": prediction_record.get("canonical_prediction_key"),
                        "prediction_source": prediction_record.get("prediction_source"),
                        "fallback_level": prediction_record.get("fallback_level"),
                        "predicted_memory_mib": prediction_record.get(
                            "predicted_incremental_memory_mib"
                        ),
                        "admitted_memory_mib": amount,
                        "local_admitted_memory_mib": local_amount,
                        "admission_memory_tier": admission_memory_tier,
                        "admission_blocked_seconds": admission_wait,
                        "endpoint_sandbox_id": route.sandbox_id,
                        "endpoint_epoch": route.epoch,
                        "endpoint_host": route.host,
                        "endpoint_port": route.port,
                    })
                    return {
                        "decision": "ADMIT", "admitted_memory_mib": amount,
                        "notify_execution_start": bool(progress_control and arm.policy.runtime_control.adaptive),
                        "local_admitted_memory_mib": local_amount,
                        "admission_memory_tier": admission_memory_tier,
                        "admission_blocked_seconds": admission_wait,
                        "sandbox_id": route.sandbox_id, "epoch": route.epoch,
                        "container_port": route.container_port,
                        "host": route.host, "port": route.port,
                    }

                def complete_openclaw_tool(request: dict[str, Any]) -> dict[str, Any]:
                    execution_id = str(request["execution_id"])
                    timestamp_names = (
                        "execution_started_at", "ssh_reaped_at", "execution_completed_at",
                    )
                    timestamps: dict[str, float] = {}
                    for name in timestamp_names:
                        value = request.get(name)
                        if isinstance(value, bool) or not isinstance(value, (int, float)):
                            raise RuntimeError(f"completion is missing numeric {name}")
                        rendered = float(value)
                        if not math.isfinite(rendered):
                            raise RuntimeError(f"completion has non-finite {name}")
                        timestamps[name] = rendered
                    if not (
                        timestamps["execution_started_at"]
                        <= timestamps["ssh_reaped_at"]
                        <= timestamps["execution_completed_at"]
                    ):
                        raise RuntimeError(
                            "completion timestamps do not prove SSH reaped before complete"
                        )
                    # A concurrent admission holds wait_lock while waiting for
                    # memory. Completion must release its finished command's
                    # reservation without that lock, or both requests deadlock.
                    with reservation_lock:
                        amount = active_reservations.get(execution_id)
                        admitted_route = admitted_routes.get(execution_id)
                        host_sampler = host_rss_samplers.get(execution_id)
                        if amount is None or admitted_route is None or host_sampler is None:
                            raise RuntimeError(f"completion has no active admission for {execution_id}")
                        completion_route = (
                            request.get("endpoint_sandbox_id"),
                            request.get("endpoint_epoch"),
                            request.get("endpoint_host"),
                            request.get("endpoint_port"),
                        )
                        expected_route = (
                            admitted_route.sandbox_id, admitted_route.epoch,
                            admitted_route.host, admitted_route.port,
                        )
                        if completion_route != expected_route:
                            raise RuntimeError(
                                "completion endpoint does not match its admission: "
                                f"expected={expected_route!r} got={completion_route!r}"
                            )
                        active_reservations.pop(execution_id)
                        admitted_routes.pop(execution_id)
                        host_rss_samplers.pop(execution_id)
                        host_observation = host_sampler.stop()
                        report = lifecycle.complete_first_tool_after_restore(
                            execution_id,
                            tool_seconds=timestamps["execution_completed_at"] - timestamps["execution_started_at"],
                            status="ok" if int(request.get("exit_code", 1)) == 0 else "error")
                        for prediction_record in prediction_records:
                            if prediction_record.get("execution_id") == execution_id:
                                prediction_record.update(host_observation)
                                increment = host_observation.get(
                                    "host_cgroup_execution_increment_bytes"
                                )
                                prediction_record["actual_host_execution_increment_mib"] = (
                                    float(increment) / (1024.0 * 1024.0)
                                    if isinstance(increment, int) else None
                                )
                                break
                        coordinator.release(session_id, amount)
                        if execution_id in active_cpu_placements:
                            priority_domain.release_tool_call(execution_id)
                            active_cpu_placements.remove(execution_id)
                        if progress_control:
                            progress_control.execution_complete(session_id, execution_id, host_observation,
                                timestamps['execution_completed_at'] - timestamps['execution_started_at'],
                                int(request.get('exit_code', 1)) == 0)
                        _record_time_span(
                            timeline, "tool.operation",
                            timestamps["execution_started_at"],
                            timestamps["execution_completed_at"],
                            role="tool", operation=str(request.get("operation") or "exec"),
                            execution_id=execution_id,
                            execution_scope=str(request.get('execution_scope') or 'agent-tool'),
                            service_seconds=(
                                timestamps["execution_completed_at"]
                                - timestamps["execution_started_at"]
                            ),
                            status=(
                                "ok" if int(request.get("exit_code", 1)) == 0
                                else "error"
                            ),
                        )

                    # Keep lifecycle/idle transitions serialized with admission.
                    # Never hold reservation_lock while waiting for wait_lock.
                    with wait_lock, reservation_lock:
                        coordinator.set_tool_active(
                            session_id,
                            bool(active_reservations or pending_admissions),
                        )
                    if report is not None:
                        events.write({"event": "first_tool_after_restore", "session_id": session_id, **report})
                    events.write({"event": "tool_completed", "session_id": session_id,
                                  "execution_id": execution_id,
                                  "exit_code": request.get("exit_code"),
                                  "ssh_reaped_at": timestamps["ssh_reaped_at"],
                                  "endpoint_epoch": expected_route[1]})
                    return {"status": "COMPLETED"}

                def abandon_openclaw_tool(
                    request: dict[str, Any], _admission: dict[str, Any],
                ) -> None:
                    """Release an admission whose HTTP response was not delivered."""
                    progress_event('complete', str(request['execution_id']))
                    execution_id = str(request["execution_id"])
                    with reservation_lock:
                        amount = active_reservations.pop(execution_id, None)
                        admitted_routes.pop(execution_id, None)
                        host_sampler = host_rss_samplers.pop(execution_id, None)
                        if amount is None or host_sampler is None:
                            raise RuntimeError(
                                f"abandoned admission has no active reservation for {execution_id}"
                            )
                        host_sampler.stop()
                        report = lifecycle.complete_first_tool_after_restore(
                            execution_id, tool_seconds=0.0,
                            status="admission_response_lost",
                        )
                        coordinator.release(session_id, amount)
                        if execution_id in active_cpu_placements:
                            priority_domain.release_tool_call(execution_id)
                            active_cpu_placements.remove(execution_id)
                    with wait_lock, reservation_lock:
                        coordinator.set_tool_active_if_registered(
                            session_id,
                            bool(active_reservations or pending_admissions),
                        )
                    if report is not None:
                        events.write({
                            "event": "first_tool_after_restore",
                            "session_id": session_id, **report,
                        })
                    events.write({
                        "event": "tool_admission_abandoned",
                        "session_id": session_id,
                        "execution_id": execution_id,
                        "reason": "response_connection_lost",
                        "released_memory_mib": amount,
                    })

                if self.policy_control is None:
                    raise RuntimeError("PolicyControlServer is not active")
                def record_tool_progress(request):
                    phase = request.get('phase')
                    progress_event('complete' if phase == 'end' else 'start',
                                   str(request['execution_id']))
                    events.write({'event': 'tool_execution_progress',
                                  'session_id': session_id,
                                  'execution_id': str(request['execution_id']),
                                  'phase': phase,
                                  'source': 'toolbridge_progress_http'})
                policy_session = self.policy_control.register(
                    session_id, admit=admit_openclaw_tool,
                    complete=complete_openclaw_tool,
                    abandon=abandon_openclaw_tool,
                    start=record_tool_progress,
                )
                # The Tool VM is authoritative for mutable state. Mark the
                # installed shared SSH runtime as initialized so OpenClaw does
                # not copy its Runtime-local workspace into the Tool.
                workspace_result = executor.execute(
                    native_tool_workspace_setup_command(arm.sandbox.workspace), 30,
                )
                if workspace_result.exit_code != 0:
                    raise RuntimeError(
                        "Tool setup could not initialize OpenClaw SSH workspace: "
                        + workspace_result.stderr[-1000:]
                    )
                tool_bridge_result = executor.execute(native_tool_bridge_setup_command(
                    require_progress_events=arm.policy.runtime_control.adaptive,
                    command_timeout_seconds=arm.execution.command_timeout_seconds,
                ), 60)
                if tool_bridge_result.exit_code != 0:
                    raise RuntimeError(
                        "Tool setup could not start native SSH bridge: "
                        + tool_bridge_result.stderr[-1000:]
                    )
                if setup_route is None:
                    raise RuntimeError("OpenClaw Tool endpoint was not resolved")
                ssh_target = setup_route.target
                events.write({
                    "event": "native_ssh_endpoint_resolved", "session_id": session_id,
                    "sandbox_id": setup_route.sandbox_id,
                    "endpoint_epoch": setup_route.epoch,
                    "target": ssh_target, "container_port": setup_route.container_port,
                    "host": setup_route.host, "port": setup_route.port,
                    "phase": "setup", "source": "cubesandbox_tcp_endpoint",
                })
                ssh_config = NativeSSHConfig(
                    target=ssh_target,
                    identity_private_key=ssh_credentials.client_private,
                    host_public_key=ssh_credentials.host_public,
                    workspace_root=OPENCLAW_SSH_RUNTIME_ROOT,
                    sandbox_id=setup_route.sandbox_id,
                    host_key_alias=native_ssh_host_key_alias(setup_route.sandbox_id),
                )

                poll_resident_runtime = RuntimeStatusPoller(
                    lifecycle=runtime_lifecycle,
                    executor=runtime_executor,
                    lifecycle_lock=wait_lock,
                    snapshot_enabled=runtime_snapshot_enabled,
                )

                outcome = run_openclaw(
                    prompt=arm.case.prompt, session_id=session_id,
                    configuration={
                        **inference_configuration,
                        "command_timeout_seconds": arm.execution.command_timeout_seconds,
                    },
                    ssh=ssh_config,
                    policy_control=policy_session, runtime_executor=runtime_executor,
                    output_dir=self.output_root,
                    timeout_seconds=arm.execution.arm_timeout_seconds,
                    model_gateway=gateway_session,
                    prediction_manifest=(prediction_provider.manifest
                                         if prediction_provider is not None else None),
                    resident_poll=poll_resident_runtime,
                    checkpoint_relay=runtime_snapshot_enabled,
                )
                if outcome.get("agent_pid_file") != agent_pid_file:
                    raise RuntimeError("OpenClaw agent PID witness path was not initialized")
                coordinator.set_tool_active(session_id, True)
                finalization_active = True
                # No Agent SSH process remains after run_openclaw returns.
                # Drain callbacks already queued by model/tool completions so
                # none can pause a VM underneath collection or validation.
                drain_policy_events()
                timeline["openclaw_agent_pid_observations"] = list(agent_pid_observations)
                tool_latencies.extend(float(item) for item in outcome["tool_latencies"])
                policy_control_path = self.output_root / "policy-control" / f"{session_id}.json"
                atomic_json(policy_control_path, outcome["policy_control_records"])
                # A completed Agent call may have triggered eager Tool eviction.
                # Serialize restore and collection with delayed model-wait and
                # eager-pause callbacks so the Tool cannot disappear halfway
                # through the artifact stream.
                with wait_lock:
                    if not lifecycle.resident:
                        self._restore_with_one_victim(
                            arm, session_id, lifecycle, coordinator, events,
                        )
                    resolve_native_ssh_route("collection")
                    if ssh_config is None:
                        raise RuntimeError("native SSH config was not initialized")
                    native_artifacts = collect_and_validate_native_tool_artifacts(
                        runtime_executor=runtime_executor, ssh=ssh_config,
                        session_id=session_id, output_dir=self.output_root,
                        policy_records=outcome["policy_control_records"],
                        runtime_trace_paths=outcome["runtime_traces"],
                    )
                    predicted_execution_ids = {
                        str(item.get("execution_id") or "")
                        for item in prediction_records
                    }
                    enrich_tool_execution_observations(
                        prediction_records, {
                            execution_id: artifact
                            for execution_id, artifact
                            in native_artifacts.cgroup_artifacts.items()
                            if execution_id in predicted_execution_ids
                        },
                    )
                events.write({
                    "event": "native_tool_artifacts_collected",
                    "session_id": session_id,
                    "root": str(native_artifacts.root),
                    "validation": native_artifacts.validation,
                })
                if gateway_session is None:
                    raise RuntimeError("managed OpenClaw session has no ModelGateway state")
                model_steps = gateway_session.gateway.logical_model_steps()
                completeness = gateway_session.replay_completeness()
                if not completeness["complete"]:
                    raise RuntimeError(
                        "managed ModelGateway session incomplete: "
                        + json.dumps(completeness, sort_keys=True)
                    )
                # OpenClaw marks a run replayInvalid after any potentially
                # mutating exec/edit, even when every Tool call succeeded.
                # The explicit Tool summary is the failure signal; task
                # correctness remains enforced by the Tool-VM validation.
                agent_tool_failures = outcome.get("agent_tool_failures")
                if agent_tool_failures is not None and agent_tool_failures > 0:
                    raise RuntimeError(
                        f"OpenClaw reported {agent_tool_failures} failed Tool result(s); "
                        "inspect final-answer.json and agent.stderr.log"
                    )
                if not tool_latencies:
                    raise RuntimeError(
                        "OpenClaw completed without using a required native Tool operation"
                    )
                if arm.inference.backend.value == "api":
                    recordings = find_recordings(outcome["runtime_traces"], session_id)
                    if len(recordings) != 1:
                        raise RuntimeError("live agent must produce one complete native model recording")
                    if len(load_trace(recordings[0])) != len(gateway_session.records()):
                        raise RuntimeError("native recording does not cover every model call")
                    events.write({
                        "event": "model_trace_recorded",
                        "session_id": session_id,
                        "paths": [str(path) for path in recordings],
                        "format": "clawtune-schema-6",
                    })
                for gateway_record in gateway_session.records():
                    request_id = str(gateway_record.get("request_id") or "")
                    _record_time_span(
                        timeline, "model.wait",
                        gateway_record.get("request_started_at"),
                        gateway_record.get("model_generated_at"),
                        role="runtime", operation="model_wait",
                        execution_id=request_id,
                    )
                    _record_time_span(
                        timeline, "model.response_hold",
                        gateway_record.get("model_generated_at"),
                        gateway_record.get("response_released_at"),
                        role="runtime", operation="response_hold",
                        execution_id=request_id,
                    )
                    _record_time_span(
                        timeline, "model.response_delivery",
                        gateway_record.get("response_released_at"),
                        gateway_record.get("response_delivered_at"),
                        role="runtime", operation="response_delivery",
                        execution_id=request_id,
                    )
                timeline["final_agent_completion"] = time.time()
            validation = arm.validation.command or (
                arm.case.validation if isinstance(arm.case.validation, str) else None)
            valid = exit_mismatches == 0
            if not finalization_active:
                coordinator.set_tool_active(session_id, True)
                finalization_active = True
            timeline.setdefault("agent_execution_start", timeline.get("sandbox_ready", time.time()))
            # A delayed eager-pause callback from the final Tool completion
            # must not swap the Tool between collection and validation. Keep
            # restore, validation, and hashing in the same lifecycle critical
            # section; Agent commands have already terminated at this point.
            with wait_lock:
                if not lifecycle.resident:
                    self._restore_with_one_victim(
                        arm, session_id, lifecycle, coordinator, events,
                    )
                timeline["validation_start"] = time.time()
                if validation:
                    validation_result = _execute_idempotent(
                        executor, validation, arm.execution.command_timeout_seconds,
                    )
                    events.write({
                        "event": "session_validation",
                        "session_id": session_id,
                        "command": validation,
                        "exit_code": validation_result.exit_code,
                        "stdout_excerpt": validation_result.stdout[-4000:],
                        "stderr_excerpt": validation_result.stderr[-4000:],
                    })
                    valid = valid and validation_result.exit_code == 0
                timeline["validation_end"] = time.time()
                timeline["output_hash_start"] = time.time()
                hash_result = _execute_idempotent(
                    executor,
                    "find . -type f -exec sha256sum {} \\; | LC_ALL=C sort | sha256sum",
                    arm.execution.command_timeout_seconds,
                )
                timeline["output_hash_end"] = time.time()
            timeline["output_hash_overhead_seconds"] = max(
                0.0, timeline["output_hash_end"] - timeline["validation_end"]
            )
            output_hash = (hash_result.stdout.split()[0] if hash_result.exit_code == 0
                           and hash_result.stdout.split() else hashlib.sha256(
                               json.dumps({"valid": valid, "mismatches": exit_mismatches},
                                          sort_keys=True).encode()).hexdigest())
            coordinator.set_tool_active(session_id, False)
            finalization_active = False
            events.write({"event": "session_complete", "session_id": session_id, "valid": valid})
            if not valid:
                raise RuntimeError(f"validation failed for {session_id}")
            events.write({
                "event": "managed_session_measurement",
                "session_id": session_id,
                "model_steps": model_steps,
                "tool_steps": len(tool_latencies),
                "model_gateway_records": (
                    gateway_session.records() if gateway_session is not None else []
                ),
                "model_gateway_completeness": (
                    gateway_session.replay_completeness()
                    if gateway_session is not None else None
                ),
                "prediction_records": prediction_records,
                "prediction_provenance": (
                    prediction_provider.provenance(prediction_records)
                    if prediction_provider is not None else None
                ),
            })
            if policy_event_errors:
                raise RuntimeError("policy event executor failed: " + "; ".join(policy_event_errors))
            return {"session_id": session_id, "case_id": arm.case.case_id,
                    "rollout_id": session_id,
                    "tool_template": arm.sandbox.template,
                    "tool_workspace": arm.sandbox.workspace,
                    "priority": priority_domain.priorities[index] if priority_domain else None,
                    "compute_node": numa_borrower.node_id if numa_borrower else None,
                    "trace_reference": (
                        arm.case.replay_trace_reference or arm.case.source_reference
                    ),
                    "valid": valid, "create_seconds": create_s,
                    "runtime_create_seconds": runtime_create_s,
                    "tool_create_seconds": tool_create_s,
                    "duration_seconds": time.monotonic() - session_started,
                    "agent_jct_seconds": max(
                        0.0, timeline["final_agent_completion"] - timeline["agent_execution_start"]
                    ),
                    "provisioning_inclusive_jct_seconds": max(
                        0.0, timeline["final_agent_completion"] - timeline["sandbox_create_start"]
                    ),
                    "validation_overhead_seconds": max(
                        0.0, timeline["validation_end"] - timeline["final_agent_completion"]
                    ),
                    "tool_steps": len(tool_latencies), "model_steps": model_steps,
                    "tool_latencies": tool_latencies, "output_hash": output_hash,
                    "prediction_records": prediction_records,
                    "prediction_provenance": (
                        prediction_provider.provenance(prediction_records)
                        if prediction_provider is not None else None
                    ),
                    "model_gateway_records": (
                        gateway_session.records() if gateway_session is not None else []
                    ),
                    "model_gateway_completeness": (
                        gateway_session.replay_completeness()
                        if gateway_session is not None else None
                    ),
                     "model_trace_path": str(model_trace_path) if model_trace_path else None,
                     "policy_control_path": (
                         str(policy_control_path) if policy_control_path else None
                     ),
                     "runtime_trace_root": (
                         str(self.output_root / "runtime-traces" / session_id)
                         if arm.agent.driver is AgentDriver.OPENCLAW else None
                     ),
                     "native_tool_artifact_root": (
                         str(native_artifacts.root)
                         if arm.agent.driver is AgentDriver.OPENCLAW and native_artifacts
                         else None
                     ),
                     "native_tool_artifact_validation": (
                         native_artifacts.validation
                         if arm.agent.driver is AgentDriver.OPENCLAW and native_artifacts
                         else None
                     ),
                     "timeline": timeline}
        finally:
            session_error = sys.exception()
            cleanup_errors: list[str] = []
            discard_failed_gateway_session = False
            discard_failed_policy_session = False

            def cleanup_step(label: str, operation: Any) -> Any:
                try:
                    return operation()
                except Exception as exc:
                    cleanup_errors.append(f"{label}: {type(exc).__name__}: {exc}")
                    return None

            if locals().get("trace_writer") is not None:
                cleanup_step("trace writer", trace_writer.close)
            if finalization_active:
                cleanup_step(
                    "tool active flag",
                    lambda: coordinator.set_tool_active(session_id, False),
                )
                finalization_active = False
            timeline["sandbox_cleanup_start"] = time.time()
            if gateway_session is not None and self.model_gateway is not None:
                gateway_drained = cleanup_step(
                    "model gateway session",
                    lambda: self.model_gateway.unregister(
                        gateway_session.token,
                        timeout=0 if session_error is not None else 30,
                    ),
                )
                if gateway_drained is False:
                    if session_error is None:
                        cleanup_errors.append("model gateway session did not drain")
                    else:
                        # unregister() has already removed this failed Agent's
                        # token.  Its in-flight request can finish only after
                        # the broken Runtime/Tool pair is stopped below, so it
                        # is isolated Agent cleanup rather than shared-arm
                        # contamination.
                        discard_failed_gateway_session = True
            if policy_session is not None:
                policy_drained = cleanup_step(
                    "policy session",
                    lambda: policy_session.close(
                        timeout=0 if session_error is not None else 30,
                    ),
                )
                if policy_drained is False:
                    if session_error is None:
                        cleanup_errors.append(
                            f"policy session did not drain: {session_id}"
                        )
                    else:
                        # A timed-out or failed Agent can leave an admitted SSH
                        # execution without a completion callback.  Stop its VMs
                        # below, then discard only this session.  The coordinator
                        # unregister still releases all of its reservations.
                        discard_failed_policy_session = True
            for host_sampler in tuple(locals().get("host_rss_samplers", {}).values()):
                cleanup_step("host RSS sampler", host_sampler.stop)
            tool_destroy_s = cleanup_step("Tool sandbox", lifecycle.close)
            if tool_destroy_s is not None:
                cleanup_step("Tool destroy event", lambda: events.write({
                    "event": "sandbox_destroyed", "session_id": session_id,
                    "role": "tool", "service_seconds": tool_destroy_s,
                    "lifecycle_timing": lifecycle.timings[-1],
                }))
            runtime_destroy_s = cleanup_step("Runtime sandbox", runtime_lifecycle.close)
            if runtime_destroy_s is not None:
                cleanup_step("Runtime destroy event", lambda: events.write({
                    "event": "sandbox_destroyed", "session_id": session_id,
                    "role": "runtime", "service_seconds": runtime_destroy_s,
                    "lifecycle_timing": runtime_lifecycle.timings[-1],
                }))
            if discard_failed_gateway_session:
                cleanup_step("failed model gateway event", lambda: events.write({
                    "event": "model_gateway_session_discarded",
                    "session_id": session_id,
                    "reason": "agent_failed_with_active_request",
                }))
            if discard_failed_policy_session:
                cleanup_step("failed policy session", policy_session.discard)
                cleanup_step("failed policy session event", lambda: events.write({
                    "event": "policy_session_discarded",
                    "session_id": session_id,
                    "reason": "agent_failed_with_incomplete_execution",
                }))
            timeline["lifecycle_timings"] = [
                {"role": "tool", **timing}
                for timing in lifecycle.timings
            ] + [
                {"role": "runtime", **timing}
                for timing in runtime_lifecycle.timings
            ]
            timeline["sandbox_cleanup_end"] = time.time()
            timeline["session_finished"] = timeline["sandbox_cleanup_end"]
            timeline["time_spans"] = build_time_spans(timeline)
            cleanup_step("session timing event", lambda: events.write({
                "event": "session_timing", "session_id": session_id,
                "time_spans": timeline["time_spans"],
            }))
            if timeline.get("validation_end"):
                timeline["cleanup_overhead_seconds"] = max(
                    0.0, timeline["sandbox_cleanup_end"] - timeline["validation_end"]
                )
            if lifetime and lifetime_acquired:
                cleanup_step(
                    "lifetime capacity",
                    lambda: coordinator.release_capacity(session_id, lifetime),
                )
            cleanup_step("coordinator session", lambda: coordinator.unregister(session_id))
            if progress_control:
                cleanup_step('pressure session', lambda: progress_control.unregister(session_id))
            if watermark_controller is not None:
                cleanup_step(
                    "watermark session", lambda: watermark_controller.unregister(session_id),
                )
            if cleanup_errors:
                failure = ArmInfrastructureError(
                    f"session cleanup incomplete for {session_id}: "
                    + "; ".join(cleanup_errors)
                )
                raise failure from sys.exception()


    def _restore_with_one_victim(self, arm: ExperimentArm, session_id: str,
                                 lifecycle: CubeSandboxLifecycle,
                                 coordinator: PolicyCoordinator, events: EventWriter,
                                 *, role: str = "tool") -> float:
        reservation_mib = arm.resources.lifecycle_reservation_mib(
            arm.runtime.memory_mib if role == "runtime" else arm.sandbox.memory_mib
        )
        elapsed, reservation_wait = coordinator.restore(
            session_id, reservation_mib, lifecycle.restore,
            arm.execution.arm_timeout_seconds,
        )
        events.write({"event": "sandbox_restored", "session_id": session_id,
                      "role": role, "service_seconds": elapsed,
                      "materialization_reservation_mib": reservation_mib,
                      "materialization_reservation_wait_seconds": reservation_wait,
                      "lifecycle_timing": lifecycle.timings[-1]})
        return elapsed

    def _prepare_tool_admission(
        self,
        arm: ExperimentArm,
        session_id: str,
        lifecycle: CubeSandboxLifecycle,
        coordinator: PolicyCoordinator,
        events: EventWriter,
        watermark_controller: WatermarkController | None,
        amount_mib: int,
        *,
        wait_for_admission: Callable[[Callable[[], float]], float],
    ) -> tuple[int, str, float, bool]:
        """Restore first, then reserve growth in its actual allocation tier."""
        coordinator.set_tool_active(session_id, True)
        restored_tool = not lifecycle.resident
        if restored_tool:
            self._restore_with_one_victim(
                arm, session_id, lifecycle, coordinator, events,
            )

        local_amount = amount_mib
        admission_memory_tier = "local"
        if (
            watermark_controller is not None
            and watermark_controller.route_progress_to_shared(lifecycle, amount_mib)
        ):
            local_amount = 0
            admission_memory_tier = "shared"

        # The caller drops its lifecycle lock during this wait so completion can
        # publish the result and release the preceding Tool claim.
        admission_wait = wait_for_admission(
            lambda: coordinator.begin_tool_admission(
                session_id,
                local_amount,
                arm.execution.arm_timeout_seconds,
                total_mib=amount_mib,
            )
        )
        return local_amount, admission_memory_tier, admission_wait, restored_tool

    def _tool_reservation_mib(self, arm: ExperimentArm, *,
                              prediction: dict[str, Any] | None = None) -> int:
        if arm.policy.admission is AdmissionPolicy.OFF:
            return 0
        if prediction and prediction.get('prediction_source') == 'non_command_fixed_host_budget':
            # This is an explicit common budget, not a model estimate. Scaling
            # it only in the predicted arm confounds admission comparisons.
            return int(arm.resources.non_command_tool_memory_mib
                       or arm.resources.static_tool_memory_mib)
        if arm.policy.admission is AdmissionPolicy.FIXED:
            return int(arm.resources.static_tool_memory_mib)
        if not prediction or prediction.get('admission_prediction_target') != 'host_cgroup_memory_current_increment':
            raise PredictionUnavailable('predicted admission requires calibrated host cgroup growth')
        value = prediction.get('predicted_incremental_memory_mib')
        if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value < 0:
            raise PredictionUnavailable('invalid predicted host memory increment')
        return max(1, math.ceil(value * arm.resources.prediction_reservation_scale))

    def _provenance(self) -> dict[str, Any]:
        def command(*args: str) -> str:
            try:
                return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()
            except (OSError, subprocess.CalledProcessError):
                return "unknown"
        try:
            sdk = importlib.metadata.version("cubesandbox")
        except importlib.metadata.PackageNotFoundError:
            sdk = "unavailable"
        cube_source = os.environ.get("CUBE_SOURCE_DIR", "")
        return {
            "cubesandbox_deployment": "standalone",
            "cubesandbox_source_directory": cube_source or None,
            "cubesandbox_tag": command("git", "-C", cube_source, "describe", "--tags", "--exact-match") if cube_source else "unknown",
            "cubesandbox_commit": command("git", "-C", cube_source, "rev-parse", "HEAD") if cube_source else "unknown",
            "cubesandbox_sdk": sdk,
            "clawbox_commit": os.environ.get("CLAWBOX_REVISION", command("git", "rev-parse", "HEAD")),
            "os_release": platform.platform(), "kernel": platform.release(),
            "architecture": platform.machine(), "cpu_model": platform.processor(),
            "template_reference": self.spec.sandbox.template,
            "template_source_image": self.spec.sandbox.source_image_reference or "unknown",
            "template_image_digest": self.spec.sandbox.image_digest or os.environ.get("CLAWBOX_TEMPLATE_IMAGE_DIGEST", "unknown"),
            # Keep the historical generic/template keys above for consumers
            # that treated them as Tool provenance, but make the two VM
            # artifacts unambiguous for new result readers.
            "runtime_template_reference": self.spec.runtime.template,
            "runtime_template_source_image": (
                self.spec.runtime.source_image_reference or "unknown"
            ),
            "runtime_template_image_digest": (
                self.spec.runtime.image_digest or "unknown"
            ),
            "tool_template_reference": self.spec.sandbox.template,
            "tool_template_source_image": (
                self.spec.sandbox.source_image_reference or "unknown"
            ),
            "tool_template_image_digest": (
                self.spec.sandbox.image_digest
                or os.environ.get("CLAWBOX_TEMPLATE_IMAGE_DIGEST", "unknown")
            ),
            "measurement_scope": "absolute VM cgroup charge; host MemAvailable estimate reported separately",
            "cube_runtime_settings": {
                "status": "unverified",
                "cpu_overcommit_ratio": None, "memory_overcommit_ratio": None,
                "paused_resource_release_ratio": None,
                "reason": "SDK does not expose effective Cubelet capacity settings",
            },
        }

    @staticmethod
    def _arm_provenance(arm: ExperimentArm) -> dict[str, Any]:
        if arm.agent.driver is AgentDriver.OPENCLAW:
            evidence_class = (
                "real-llm" if arm.inference.backend.value == "api"
                else "deterministic-managed-replay"
            )
        else:
            evidence_class = "synthetic-stress"
        cases = arm.session_cases or (arm.case,)
        configured_traces = []
        for case in cases:
            trace_reference = case.replay_trace_reference or case.source_reference
            trace_path = Path(trace_reference)
            configured_traces.append({
                "case_id": case.case_id,
                "reference": trace_reference,
                "sha256": (
                    hashlib.sha256(trace_path.read_bytes()).hexdigest()
                    if trace_path.is_file() else None
                ),
            })
        configuration = arm.inference.configuration
        assigned_cases = [session_case_for(arm, index) for index in range(arm.concurrency)]
        tool_memory_mib = [
            (case.sandbox or arm.sandbox).memory_mib for case in assigned_cases
        ]
        pair_memory_mib = [arm.runtime.memory_mib + value for value in tool_memory_mib]
        offered_provisioned_memory_mib = sum(pair_memory_mib)
        pool_memory_budget_mib = arm.resources.pool_memory_budget_mib
        return {
            "evidence_class": evidence_class,
            "workload_case_ids": [case.case_id for case in cases],
            "configured_traces": configured_traces,
            "configured_trace_reference": (
                configured_traces[0]["reference"] if len(configured_traces) == 1 else None
            ),
            "configured_trace_sha256": (
                configured_traces[0]["sha256"] if len(configured_traces) == 1 else None
            ),
            "session_assignment": arm.session_assignment.value,
            "random_seed": arm.execution.random_seed,
            "arrival_schedule": {
                "kind": arm.execution.arrival_schedule.value,
                "stagger_interval_seconds": arm.execution.stagger_interval_seconds,
            },
            "resource_scope": {
                "target_node": arm.resources.target_node,
                "pool_memory_budget_mib": pool_memory_budget_mib,
                "emergency_free_memory_mib": arm.resources.emergency_free_memory_mib,
                "configured_checkpoint_restore_headroom_mib": (
                    arm.resources.checkpoint_restore_headroom_mib
                ),
                "effective_checkpoint_restore_headroom_mib": (
                    policy_operation_headroom_mib(arm)
                ),
                "runtime_memory_mib_per_agent": arm.runtime.memory_mib,
                "tool_memory_mib_per_agent": (
                    tool_memory_mib[0] if len(set(tool_memory_mib)) == 1 else None
                ),
                "pair_memory_mib_per_agent": (
                    pair_memory_mib[0] if len(set(pair_memory_mib)) == 1 else None
                ),
                "session_tool_memory_mib": tool_memory_mib,
                "session_pair_memory_mib": pair_memory_mib,
                "tool_memory_mib_by_case": {
                    case.case_id: (case.sandbox or arm.sandbox).memory_mib
                    for case in cases
                },
                "offered_provisioned_memory_mib": offered_provisioned_memory_mib,
                "offered_to_policy_pool_ratio": (
                    offered_provisioned_memory_mib / pool_memory_budget_mib
                ),
                "offered_provisioned_capacity_exceeds_policy_budget": (
                    offered_provisioned_memory_mib > pool_memory_budget_mib
                ),
            },
            "model_provenance": {
                "backend": arm.inference.backend.value,
                "model": configuration.get("model"),
                "base_url": configuration.get("base_url"),
                "time_scale": configuration.get("time_scale"),
                "credential_environment": configuration.get("api_key_env"),
            },
        }

    def _write_summary(self) -> None:
        write_summary(
            self.output_root, run_id=self.run_id,
            attempt_id=self.attempt_id, task_uid=self.task_uid,
            experiment_id=self.spec.experiment_id, results=self.results,
        )


def write_summary(output_root: Path, *, run_id: str, attempt_id: str,
                  task_uid: str, experiment_id: str,
                  results: list[ResultEnvelope]) -> None:
        summary = {"run_id": run_id, "attempt_id": attempt_id,
                   "experiment_id": experiment_id,
                   "arms": [item.model_dump(mode="json") for item in results]}
        atomic_json(output_root / "summary.json", summary)
        path = output_root / "summary.csv"
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["arm_id", "policy", "concurrency",
                                                        "repetition", "status", "duration_seconds"])
            writer.writeheader()
            for item in results:
                writer.writerow({"arm_id": item.arm.arm_id, "policy": item.arm.policy.name,
                                 "concurrency": item.arm.concurrency, "repetition": item.arm.repetition,
                                 "status": item.status, "duration_seconds": item.performance.get("duration_seconds")})
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        lines = [
            f"# Experiment {experiment_id}", "",
            f"Run: `{run_id}`  ",
            f"Attempt: `{attempt_id}`  ",
            f"SandboxTask UID: `{task_uid}`", "",
            "| Arm | Policy | Agents | Status | Duration (s) | Pauses | Resumes |",
            "|---|---|---:|---|---:|---:|---:|",
        ]
        for item in results:
            lines.append(
                f"| `{item.arm.arm_id}` | {item.arm.policy.name} | {item.arm.concurrency} | "
                f"{item.status.value} | {item.performance.get('duration_seconds', 0):.3f} | "
                f"{item.performance.get('pause_count', 0)} | {item.performance.get('resume_count', 0)} |"
            )
        markdown = output_root / "summary.md"
        markdown_tmp = markdown.with_name(markdown.name + ".tmp")
        markdown_tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(markdown_tmp, markdown)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one ClawBox experiment attempt")
    parser.add_argument("--spec", type=Path, default=Path(os.environ.get("CLAWBOX_EXPERIMENT_SPEC", "/config/experiment.yaml")))
    parser.add_argument("--run-id", default=os.environ.get("CLAWBOX_RUN_ID"), required=os.environ.get("CLAWBOX_RUN_ID") is None)
    parser.add_argument("--attempt-id", default=os.environ.get("CLAWBOX_ATTEMPT_ID"), required=os.environ.get("CLAWBOX_ATTEMPT_ID") is None)
    parser.add_argument("--task-uid", default=os.environ.get("CLAWBOX_TASK_UID"), required=os.environ.get("CLAWBOX_TASK_UID") is None)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--arm-id")
    parser.add_argument("--no-summary", action="store_true")
    args = parser.parse_args(argv)
    try:
        results = ExperimentWorker(
            load_experiment(args.spec), run_id=args.run_id,
            attempt_id=args.attempt_id, task_uid=args.task_uid,
            output_root=args.output_root,
        ).run(
            arm_ids={args.arm_id} if args.arm_id else None,
            write_summary=not args.no_summary,
        )
    except Exception as exc:
        # Preserve a useful diagnostic when a pre-arm infrastructure gate
        # fails. Keep the log useful without
        # printing credentials, prompts, request bodies, or bearer tokens.
        print(
            "experiment worker failed before completion: "
            f"run_id={args.run_id} attempt_id={args.attempt_id} "
            f"task_uid={args.task_uid} error_type={type(exc).__name__} "
            f"error={exc}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0 if all(result.status is RunStatus.SUCCEEDED for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
