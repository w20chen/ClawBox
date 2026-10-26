from __future__ import annotations

import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from enum import Enum
from threading import RLock
from collections.abc import Callable
from typing import Any, Mapping

from clawbox.replay.lifecycle import LifecycleError

from .client import CubeSandboxClient, Ownership
from clawbox.experiments.snapshot_pool import SnapshotKey, SnapshotManifest, WarmSnapshotPool, WarmCapacityError
from clawbox.experiments.spec_types import SnapshotTier


class SandboxState(str, Enum):
    NEW = "new"
    CREATING = "creating"
    RUNNING = "running"
    CHECKPOINTING = "checkpointing"
    UNKNOWN = "unknown"
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
    host_memory_before: dict[str, Any] | None = None
    host_memory_after: dict[str, Any] | None = None
    host_observed_reclaimed_bytes: int | None = None
    host_observed_growth_bytes: int | None = None
    host_observed_net_change_bytes: int | None = None
    host_reclamation_evidence: str = "unavailable"
    lifecycle_mechanism: str | None = None
    tier_from: str | None = None
    tier_to: str | None = None
    snapshot_generation: int | None = None
    snapshot_path: str | None = None
    snapshot_metrics: dict[str, Any] | None = None
    host_memory_effect: dict[str, dict[str, int]] | None = None
    stage_seconds: dict[str, float] | None = None


class CubeSandboxLifecycle:
    """Concurrency-safe policy layer over official CubeSandbox lifecycle calls."""

    def __init__(self, client: CubeSandboxClient, *, template: str,
                 node_name: str, ownership: Ownership,
                 allow_internet_access: bool = True,
                 env_vars: Mapping[str, str] | None = None,
                 network_allow_out: list[str] | None = None,
                 network_deny_out: list[str] | None = None,
                 physical_observation: Callable[[], Mapping[str, Any]] | None = None,
                 role: str = "tool", warm_snapshot_root: str | None = None,
                 cold_snapshot_root: str | None = None,
                 snapshot_pool: WarmSnapshotPool | None = None,
                 snapshot_reservation_bytes: int | None = None,
                 snapshot_mechanism: str = "incremental-cow", lazy_restore: bool = True,
                 snapshot_storage: str = "tiered", numa_borrower: Any = None,
                 configured_memory_bytes: int | None = None,
                 cgroup_token: str | None = None,
                 verify_cgroup: Callable[[str], Any] | None = None,
                 prepare_cgroup: Callable[[], Any] | None = None,
                 default_snapshot_tier: SnapshotTier | None = None,
                 snapshot_reuse_probability: float | None = None,
                 warm_restore_seconds_per_gib: float | None = None,
                 cold_restore_seconds_per_gib: float | None = None) -> None:
        if snapshot_storage not in {"tiered", "warm-only"}:
            raise ValueError("invalid snapshot storage")
        self.snapshot_storage = snapshot_storage
        if snapshot_mechanism not in {"full-copy", "incremental-cow"}:
            raise ValueError("invalid snapshot mechanism")
        if snapshot_mechanism == "incremental-cow" and not lazy_restore:
            raise ValueError("incremental-cow requires lazy restore")
        self.snapshot_mechanism = snapshot_mechanism
        self.lazy_restore = lazy_restore
        self._snapshot_tier: SnapshotTier | None = None
        self.client = client
        self.template = template
        self.node_name = node_name
        self.ownership = ownership
        self.allow_internet_access = allow_internet_access
        self.env_vars = dict(env_vars or {})
        if cgroup_token:
            self.env_vars['CLAWBOX_CGROUP_TOKEN'] = cgroup_token
        self.verify_cgroup = verify_cgroup
        self.prepare_cgroup = prepare_cgroup
        self.network_allow_out = list(network_allow_out or [])
        self.network_deny_out = list(network_deny_out or [])
        self.physical_observation = physical_observation
        self.role = role
        self.warm_snapshot_root = warm_snapshot_root
        self.cold_snapshot_root = cold_snapshot_root
        self.snapshot_pool = snapshot_pool
        self.default_snapshot_tier = default_snapshot_tier
        self.snapshot_reuse_probability = snapshot_reuse_probability
        self.warm_restore_seconds_per_gib = warm_restore_seconds_per_gib
        self.cold_restore_seconds_per_gib = cold_restore_seconds_per_gib
        self.snapshot_reservation_bytes = snapshot_reservation_bytes
        self.numa_borrower = numa_borrower
        self.configured_memory_bytes = configured_memory_bytes
        self.sandbox = None
        self.sandbox_id: str | None = None
        self._state = SandboxState.NEW
        self._lock = RLock()
        self._timings: list[LifecycleTiming] = []
        self._tier = SnapshotTier.LOCAL
        self._generation = 0
        self._snapshot_key: SnapshotKey | None = None
        self._pending_snapshot_keys: set[SnapshotKey] = set()
        self._warm_paths: set[str] = set()
        self._first_tool_restore: dict[str, Any] | None = None
        self._restore_fault_sampler = None

    def claim_first_tool_after_restore(self, execution_id: str) -> None:
        """Claim at admission, so concurrent completion order cannot relabel it."""
        with self._lock:
            marker = self._first_tool_restore
            if marker is not None and marker.get("execution_id") is None:
                marker["execution_id"] = execution_id

    def complete_first_tool_after_restore(self, execution_id: str, *,
                                         tool_seconds: float, status: str) -> dict[str, Any] | None:
        with self._lock:
            marker = self._first_tool_restore
            if marker is None or marker.get("execution_id") != execution_id:
                return None
            completed = time.monotonic()
            faults = self._restore_fault_sampler.stop() if self._restore_fault_sampler else {}
            self._restore_fault_sampler = None
            self._first_tool_restore = None
            return {
                **marker,
                "first_tool_after_restore_seconds": max(0.0, tool_seconds),
                "restore_ready_to_first_tool_complete_seconds": max(
                    0.0, completed - marker["restore_ready_monotonic_s"]),
                "status": status,
                **{key: value for key, value in faults.items()
                   if key in {"host_vm_minor_faults_delta", "host_vm_major_faults_delta",
                              "host_vm_disk_read_bytes_delta"}},
                "fault_counter_scope": "host_vm_processes_restore_ready_to_first_tool_complete",
            }

    @property
    def state(self) -> SandboxState:
        with self._lock:
            return self._state

    @property
    def resident(self) -> bool:
        # A single-state observation is not a transition guard. Admission and
        # victim selection call this while holding their global condition;
        # waiting for a slow cpuset rebind/RPC here stalls unrelated sessions.
        # Lifecycle operations still acquire _lock and recheck their state.
        return self._state is SandboxState.RUNNING

    @property
    def tier(self) -> SnapshotTier:
        with self._lock:
            return self._tier

    @property
    def timings(self) -> list[dict[str, object]]:
        with self._lock:
            return [asdict(item) for item in self._timings]

    def _record(self, operation: str, before: SandboxState, after: SandboxState,
                started_wall: float, started_mono: float, *,
                status: str = "ok", error_type: str | None = None,
                host_memory_before: Mapping[str, Any] | None = None,
                tier_from: str | None = None, tier_to: str | None = None,
                snapshot_generation: int | None = None,
                snapshot_path: str | None = None,
                snapshot_metrics: Mapping[str, Any] | None = None,
                stage_seconds: Mapping[str, float] | None = None) -> float:
        completed_wall = time.time()
        completed_mono = time.monotonic()
        duration = max(0.0, completed_mono - started_mono)
        measured_stages = dict(stage_seconds or {})
        observation_started = time.monotonic()
        try:
            host_memory_after = self._observe_physical()
        finally:
            measured_stages["physical_observation_after_s"] = max(
                0.0, time.monotonic() - observation_started,
            )
        before_used = self._host_used(host_memory_before)
        after_used = self._host_used(host_memory_after)
        reclaimed = growth = net_change = None
        evidence = "unavailable"
        if before_used is not None and after_used is not None:
            reclaimed = max(0, before_used - after_used)
            growth = max(0, after_used - before_used)
            net_change = after_used - before_used
            if operation == "checkpoint":
                scope = ("local_cgroup" if host_memory_before is not None and
                         "local_used_bytes" in host_memory_before else "whole_host")
                evidence = (
                    f"observed_{scope}_reclamation"
                    if reclaimed > 0
                    else f"not_observed_{scope}_reclamation"
                )
        memory_effect: dict[str, dict[str, int]] = {}
        if host_memory_before is not None and host_memory_after is not None:
            for key in (
                "local_used_bytes", "shared_live_used_bytes",
                "combined_live_used_bytes", "host_used_bytes",
            ):
                before_value = host_memory_before.get(key)
                after_value = host_memory_after.get(key)
                if (isinstance(before_value, int) and not isinstance(before_value, bool)
                        and isinstance(after_value, int) and not isinstance(after_value, bool)):
                    memory_effect[key] = {
                        "before_bytes": before_value,
                        "after_bytes": after_value,
                        "net_change_bytes": after_value - before_value,
                        "reclaimed_bytes": max(0, before_value - after_value),
                        "growth_bytes": max(0, after_value - before_value),
                    }
        self._timings.append(LifecycleTiming(
            operation, started_wall, completed_wall, started_mono,
            completed_mono, duration, before.value, after.value,
            status, error_type,
            dict(host_memory_before) if host_memory_before is not None else None,
            dict(host_memory_after) if host_memory_after is not None else None,
            reclaimed, growth, net_change, evidence,
            "cubesandbox_pause_snapshot_destroy" if operation == "checkpoint" else None,
            tier_from, tier_to, snapshot_generation, snapshot_path,
            dict(snapshot_metrics) if snapshot_metrics is not None else None,
            memory_effect or None,
            measured_stages or None,
        ))
        return duration

    @contextmanager
    def _measure_stage(self, timings: dict[str, float], name: str):
        started = time.monotonic()
        try:
            yield
        finally:
            timings[name] = max(0.0, time.monotonic() - started)

    def _observe_physical(self) -> Mapping[str, Any] | None:
        return self.physical_observation() if self.physical_observation is not None else None

    @staticmethod
    def _host_used(sample: Mapping[str, Any] | None) -> int | None:
        if sample is None:
            return None
        value = sample.get("local_used_bytes", sample.get("host_used_bytes"))
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

    def start(self) -> float:
        with self._lock:
            if self._state is not SandboxState.NEW:
                raise LifecycleError("CubeSandbox lifecycle already started")
            before = self._state
            self._state = SandboxState.CREATING
            started_wall, started_mono = time.time(), time.monotonic()
            stage_seconds: dict[str, float] = {}
            with self._measure_stage(stage_seconds, "physical_observation_before_s"):
                host_memory_before = self._observe_physical()
            try:
                if self.prepare_cgroup is not None:
                    with self._measure_stage(stage_seconds, "prepare_cgroup_s"):
                        self.prepare_cgroup()
                # Bound concurrent lifecycle calls and measure queue time
                # separately from the actual Cube API operation.
                with self._transfer("create", stage_seconds):
                    with self._measure_stage(stage_seconds, "api_create_s"):
                        self.sandbox = self.client.create_sandbox(
                            template=self.template, node_name=self.node_name,
                            ownership=self.ownership, env_vars=self.env_vars or None,
                            allow_internet_access=self.allow_internet_access,
                            network_allow_out=self.network_allow_out or None,
                            network_deny_out=self.network_deny_out or None,
                        )
                self.sandbox_id = self.client.sandbox_id(self.sandbox)
                verified = None
                if self.verify_cgroup is not None:
                    with self._measure_stage(stage_seconds, "verify_cgroup_s"):
                        verified = self.verify_cgroup(self.sandbox_id)
                if self.numa_borrower is not None:
                    with self._measure_stage(stage_seconds, "pin_local_s"):
                        verified_cgroup = verified.get('cgroup') if isinstance(verified, Mapping) else None
                        placement = (
                            verified if isinstance(verified, Mapping)
                            and isinstance(verified.get('actual_residency_bytes'), Mapping)
                            and isinstance(verified.get('actual_anon_residency_bytes'), Mapping)
                            else None
                        )
                        if verified_cgroup is None:
                            self.numa_borrower.pin_local(self.sandbox_id)
                        else:
                            self.numa_borrower.pin_local(
                                self.sandbox_id, verified_cgroup=verified_cgroup,
                                **({'verified_placement': placement} if placement else {}),
                            )
                self._state = SandboxState.RUNNING
                return self._record("create", before, self._state,
                                    started_wall, started_mono,
                                    host_memory_before=host_memory_before,
                                    stage_seconds=stage_seconds)
            except Exception as exc:
                self._state = before
                self._record(
                    "create", before, before, started_wall, started_mono,
                    status="error", error_type=type(exc).__name__,
                    host_memory_before=host_memory_before,
                    stage_seconds=stage_seconds,
                )
                raise

    @contextmanager
    def reserve_checkpoint(self, *, tier: SnapshotTier | None = None):
        """Reserve WARM capacity before any member of an idle VM pair pauses.

        The caller enters both lifecycles' contexts before checkpointing either.
        Failed pair admission therefore releases reservations without pausing a VM.
        """
        with self._lock:
            target = tier or self.default_snapshot_tier
            if self.snapshot_storage == "warm-only":
                if target is SnapshotTier.COLD:
                    raise ValueError("COLD snapshots are disabled by warm-only storage")
                target = SnapshotTier.WARM
            key = None
            if self.resident and target is SnapshotTier.WARM:
                if self.snapshot_pool is None or self.snapshot_reservation_bytes is None:
                    raise RuntimeError("WARM snapshot accounting is not configured")
                key = SnapshotKey(self.ownership.session_id, self.role, self._generation + 1)
                self.snapshot_pool.reserve_for_admission(
                    key, self.snapshot_reservation_bytes, timeout_s=0,
                    retention_value_seconds=self._retention_value_seconds(
                        self.configured_memory_bytes or self.snapshot_reservation_bytes),
                )
                self._pending_snapshot_keys.add(key)
            try:
                yield
            finally:
                if (key is not None and key in self._pending_snapshot_keys
                        and self._state is SandboxState.RUNNING):
                    self.snapshot_pool.abort(key)
                    self._pending_snapshot_keys.discard(key)

    def checkpoint_and_evict(self, *, tier: SnapshotTier | None = None) -> float | None:
        """Ask CubeSandbox to snapshot the VM and destroy its live runtime.

        CubeSandbox commit 64102d9 implements its pause API as
        ``PauseToSnapshot`` followed by a keep-tombstone destroy. The API
        completion is still not, by itself, physical-memory evidence; the
        before/after host observations below remain explicit in every record.
        """
        with self._lock:
            # Pressure eviction and the session's delayed eager callback can
            # converge on the same idle VM. The lifecycle lock serializes the
            # calls; the loser observes the already-swapped state and treats
            # the desired state as satisfied instead of failing an unrelated
            # Tool admission.
            if self._state is SandboxState.SWAPPED:
                return None
            if self.sandbox is None or self._state is not SandboxState.RUNNING:
                raise LifecycleError("CubeSandbox is not resident")
            if tier is None:
                tier = self.default_snapshot_tier
            if self.snapshot_storage == "warm-only":
                if tier is SnapshotTier.COLD:
                    raise ValueError("COLD snapshots are disabled by warm-only storage")
                tier = SnapshotTier.WARM
            if tier is None and self.snapshot_mechanism == "incremental-cow":
                tier = SnapshotTier.COLD
            if tier is SnapshotTier.COLD and not self.cold_snapshot_root:
                raise ValueError("COLD checkpoint requires cold_snapshot_root")
            before = self._state
            self._state = SandboxState.CHECKPOINTING
            started_wall, started_mono = time.time(), time.monotonic()
            stage_seconds: dict[str, float] = {}
            host_memory_before = self._observe_physical()
            key = None
            pause_attempted = False
            pause_completed = False
            path = None
            predecessor = (self._snapshot_key if self._snapshot_tier is SnapshotTier.WARM
                           and self._tier is SnapshotTier.LOCAL else None)
            try:
                manifest = None
                # A disabled or undersized WARM pool is the SSD-only ablation,
                # not a failed checkpoint. Keep the same placement policy but
                # materialize this generation in COLD when it cannot fit WARM.
                if (tier is SnapshotTier.WARM and self.snapshot_pool is not None
                        and self.snapshot_reservation_bytes is not None
                        and self.snapshot_reservation_bytes > self.snapshot_pool.capacity_bytes):
                    if self.snapshot_storage == "warm-only":
                        raise WarmCapacityError("WARM pool is too small; disk fallback is disabled")
                    tier = SnapshotTier.COLD
                if tier is None:
                    with self._transfer("checkpoint", stage_seconds), self._measure_stage(stage_seconds, "api_checkpoint_s"):
                        pause_attempted = True
                        self.client.pause_sandbox(
                            self.sandbox, snapshot_mechanism=self.snapshot_mechanism,
                        )
                        pause_completed = True
                else:
                    if tier is SnapshotTier.LOCAL:
                        raise ValueError("cannot checkpoint a sandbox to LOCAL")
                    self._generation += 1
                    key = SnapshotKey(self.ownership.session_id, self.role, self._generation)
                    if tier is SnapshotTier.WARM:
                        if self.snapshot_pool is None or self.snapshot_reservation_bytes is None:
                            raise RuntimeError("WARM snapshot accounting is not configured")
                        try:
                            retention_value = self._retention_value_seconds(
                                self.configured_memory_bytes
                                or self.snapshot_reservation_bytes
                            )
                            if key not in self._pending_snapshot_keys:
                                self.snapshot_pool.reserve_for_admission(
                                    key, self.snapshot_reservation_bytes,
                                    timeout_s=0 if predecessor is not None or self.default_snapshot_tier is not None else 120,
                                    retention_value_seconds=retention_value,
                                )
                            self._pending_snapshot_keys.add(key)
                        except WarmCapacityError:
                            if self.snapshot_storage == "warm-only":
                                raise
                            tier = SnapshotTier.COLD
                    root = (self.warm_snapshot_root if tier is SnapshotTier.WARM
                            else self.cold_snapshot_root)
                    if not root or self.sandbox_id is None:
                        raise RuntimeError(f"{tier.value.upper()} snapshot root is not configured")
                    path = (
                        f"{root.rstrip('/')}/{self.sandbox_id}/"
                        f"{self.role}-g{self._generation}.mem"
                    )
                    if tier is SnapshotTier.WARM:
                        self._warm_paths.add(path)
                    with self._transfer("checkpoint", stage_seconds), self._measure_stage(stage_seconds, "api_checkpoint_s"):
                        pause_attempted = True
                        manifest = self.client.pause_sandbox(
                            self.sandbox, tier=tier.value,
                            memory_snapshot_path=path, generation=self._generation,
                            snapshot_mechanism=self.snapshot_mechanism,
                        )
                        pause_completed = True
                    if tier is SnapshotTier.WARM:
                        self.snapshot_pool.commit(
                            key, path=path,
                            logical_bytes=int(manifest["logical_bytes"]),
                            allocated_bytes=int(manifest["allocated_bytes"]),
                            transferred_bytes=(
                                int(manifest["transferred_bytes"])
                                if manifest["transferred_bytes"] is not None else None
                            ),
                            spiller=(self._spill_to_cold if self.snapshot_storage != "warm-only" else None),
                            replaces=predecessor,
                            template_id=self.template,
                            shared_base_identity=manifest.get("external_base_identity"),
                            shared_base_allocated_bytes=int(
                                manifest.get("external_base_allocated_bytes") or 0
                            ),
                            private_delta_bytes=int(
                                manifest.get("private_delta_bytes") or manifest["allocated_bytes"]
                            ),
                            dependency_count=int(manifest.get("dependency_count") or 0),
                            expected_reuse_probability=self.snapshot_reuse_probability,
                            retention_value_seconds=self._retention_value_seconds(
                                int(manifest["logical_bytes"])
                            ),
                        )
                        self._pending_snapshot_keys.discard(key)
                    elif predecessor is not None and self.snapshot_pool is not None:
                        self._verify_warm_files_removed()
                        self.snapshot_pool.remove(predecessor)
                    self._snapshot_key = key
                    self._snapshot_tier = tier
                    self._tier = tier
                self._state = SandboxState.SWAPPED
                if self.numa_borrower is not None and self.sandbox_id is not None:
                    # Pause keeps a Cube tombstone/shim for resume, so its
                    # cgroup may remain populated even though guest memory is
                    # gone. Restore LOCAL placement and retain only any
                    # reservation still backed by measured shared residency.
                    if self.sandbox_id in self.numa_borrower.borrowed():
                        self.numa_borrower.return_local(self.sandbox_id)
                return self._record("checkpoint", before, self._state,
                                    started_wall, started_mono,
                                    host_memory_before=host_memory_before,
                                    tier_from=SnapshotTier.LOCAL.value,
                                    stage_seconds=stage_seconds,
                                    tier_to=tier.value if tier is not None else None,
                                    snapshot_generation=key.generation if key is not None else None,
                                    snapshot_path=manifest.get("memory_snapshot_path") if manifest else None,
                                    snapshot_metrics=manifest)
            except Exception as exc:
                if key is not None and self.snapshot_pool is not None and not pause_attempted:
                    self.snapshot_pool.abort(key)
                    self._pending_snapshot_keys.discard(key)
                # A pause can commit remotely and then fail while returning or
                # validating its manifest. Do not admit commands to a VM whose
                # actual state is paused or cannot be determined.
                remote_state = None
                if pause_attempted and self.sandbox_id is not None:
                    try:
                        remote_state = self.client.get_sandbox_state(self.sandbox)
                    except Exception:
                        pass
                # Cube can finish PauseToSnapshot and lose or time out the
                # control response under a large lifecycle wave.  The paused
                # state plus Cube's complete native snapshot metadata are the
                # authoritative completion evidence; publish that generation
                # instead of stranding the VM in UNKNOWN and leaking its WARM
                # reservation.  Never use this path for a transfer-slot wait
                # or for a failure after the manifest was already returned.
                if (
                    pause_attempted and not pause_completed
                    and getattr(exc, "status_code", None) is None
                    and remote_state == "paused"
                    and key is not None and tier is not None and path is not None
                ):
                    try:
                        from .snapshot import read_snapshot_metadata
                        manifest = {
                            "memory_snapshot_path": path,
                            "generation": key.generation,
                            "tier": tier.value,
                            **read_snapshot_metadata(
                                path,
                                require_lineage=self.snapshot_mechanism == "incremental-cow",
                            ),
                            "control_response_reconciled": True,
                        }
                        manifest.setdefault("transferred_bytes", None)
                        if tier is SnapshotTier.WARM:
                            if self.snapshot_pool is None:
                                raise RuntimeError("WARM snapshot accounting is not configured")
                            self.snapshot_pool.commit(
                                key, path=path,
                                logical_bytes=int(manifest["logical_bytes"]),
                                allocated_bytes=int(manifest["allocated_bytes"]),
                                transferred_bytes=(
                                    int(manifest["transferred_bytes"])
                                    if manifest["transferred_bytes"] is not None else None
                                ),
                                spiller=(self._spill_to_cold if self.snapshot_storage != "warm-only" else None),
                                replaces=predecessor,
                                template_id=self.template,
                                shared_base_identity=manifest.get("external_base_identity"),
                                shared_base_allocated_bytes=int(
                                    manifest.get("external_base_allocated_bytes") or 0
                                ),
                                private_delta_bytes=int(
                                    manifest.get("private_delta_bytes") or manifest["allocated_bytes"]
                                ),
                                dependency_count=int(manifest.get("dependency_count") or 0),
                                expected_reuse_probability=self.snapshot_reuse_probability,
                                retention_value_seconds=self._retention_value_seconds(
                                    int(manifest["logical_bytes"])
                                ),
                            )
                            self._pending_snapshot_keys.discard(key)
                        elif predecessor is not None and self.snapshot_pool is not None:
                            self._verify_warm_files_removed()
                            self.snapshot_pool.remove(predecessor)
                        self._snapshot_key = key
                        self._snapshot_tier = tier
                        self._tier = tier
                        self._state = SandboxState.SWAPPED
                        if (
                            self.numa_borrower is not None
                            and self.sandbox_id is not None
                            and self.sandbox_id in self.numa_borrower.borrowed()
                        ):
                            self.numa_borrower.return_local(self.sandbox_id)
                        return self._record(
                            "checkpoint", before, self._state,
                            started_wall, started_mono,
                            host_memory_before=host_memory_before,
                            tier_from=SnapshotTier.LOCAL.value,
                            tier_to=tier.value,
                            snapshot_generation=key.generation,
                            snapshot_path=path,
                            snapshot_metrics=manifest,
                            stage_seconds=stage_seconds,
                        )
                    except Exception:
                        # If either half of the proof is absent, retain the
                        # original failure and the conservative UNKNOWN state.
                        pass
                self._state = (before if not pause_attempted or remote_state == "running"
                               else SandboxState.UNKNOWN)
                self._record(
                    "checkpoint", before, self._state, started_wall, started_mono,
                    status="error", error_type=type(exc).__name__,
                    host_memory_before=host_memory_before,
                    stage_seconds=stage_seconds,
                )
                raise

    def _retention_value_seconds(self, state_bytes: int) -> float:
        """Expected restore-time saving for logical VM state.

        WARM capacity is charged by private allocated bytes, while restore cost
        is calibrated per GiB of logical guest state. Keeping those quantities
        separate lets value density reward a small Delta that avoids restoring
        a much larger self-contained COLD Base.
        """
        if (
            self.snapshot_reuse_probability is None
            or self.warm_restore_seconds_per_gib is None
            or self.cold_restore_seconds_per_gib is None
        ):
            return 0.0
        saved_per_gib = max(
            0.0,
            self.cold_restore_seconds_per_gib - self.warm_restore_seconds_per_gib,
        )
        return (
            self.snapshot_reuse_probability * saved_per_gib
            * state_bytes / 1024**3
        )

    def _spill_to_cold(self, manifest: SnapshotManifest) -> None:
        with self._lock:
            if (self._state is not SandboxState.SWAPPED or self.sandbox is None or
                    self._snapshot_key != manifest.key or self._tier is not SnapshotTier.WARM):
                raise LifecycleError(
                    "WARM spill target is not the authoritative swapped generation"
                )
            if not self.cold_snapshot_root or self.sandbox_id is None:
                raise RuntimeError("COLD snapshot root is not configured")
            destination = (
                f"{self.cold_snapshot_root.rstrip('/')}/{self.sandbox_id}/"
                f"{self.role}-g{manifest.key.generation}.mem"
            )
            before = self._state
            started_wall, started_mono = time.time(), time.monotonic()
            host_memory_before = self._observe_physical()
            try:
                with self._transfer("spill"):
                    self.client.relocate_snapshot(
                        self.sandbox, tier=SnapshotTier.COLD.value,
                        memory_snapshot_path=destination,
                        generation=manifest.key.generation,
                    )
                self._verify_warm_files_removed()
                self._tier = SnapshotTier.COLD
                self._snapshot_tier = SnapshotTier.COLD
                self._record(
                    "spill", before, before, started_wall, started_mono,
                    host_memory_before=host_memory_before,
                    tier_from=SnapshotTier.WARM.value,
                    tier_to=SnapshotTier.COLD.value,
                    snapshot_generation=manifest.key.generation,
                    snapshot_path=destination,
                )
            except Exception as exc:
                self._record(
                    "spill", before, before, started_wall, started_mono,
                    status="error", error_type=type(exc).__name__,
                    host_memory_before=host_memory_before,
                    tier_from=SnapshotTier.WARM.value,
                    tier_to=SnapshotTier.COLD.value,
                    snapshot_generation=manifest.key.generation,
                    snapshot_path=destination,
                )
                raise

    pause_and_evict = checkpoint_and_evict

    @contextmanager
    def _snapshot_access(self):
        # Do not hold this lifecycle's lock while waiting for an in-flight
        # spill: its callback acquires the same lock to publish COLD state.
        while True:
            with self._lock:
                key, pool = self._snapshot_key, self.snapshot_pool
            guard = pool.consume_guard(key) if key is not None and pool else nullcontext()
            with guard, self._lock:
                if key != self._snapshot_key:
                    continue
                yield
                return

    def restore(self) -> float:
        with self._snapshot_access():
            if self.sandbox_id is None or self._state is not SandboxState.SWAPPED:
                raise LifecycleError("CubeSandbox is not swapped out")
            before = self._state
            self._state = SandboxState.RESTORING
            started_wall, started_mono = time.time(), time.monotonic()
            stage_seconds: dict[str, float] = {}
            with self._measure_stage(stage_seconds, "physical_observation_before_s"):
                host_memory_before = self._observe_physical()
            resume_attempted = False
            try:
                resume_attempted = True
                if self.prepare_cgroup is not None:
                    with self._measure_stage(stage_seconds, "prepare_cgroup_s"):
                        self.prepare_cgroup()
                with self._transfer("restore", stage_seconds), self._measure_stage(stage_seconds, "api_restore_s"):
                    self.sandbox = self.client.connect_sandbox(
                        self.sandbox_id,
                        snapshot_mechanism=self.snapshot_mechanism,
                    )
                verified = None
                if self.verify_cgroup is not None:
                    with self._measure_stage(stage_seconds, "verify_cgroup_s"):
                        verified = self.verify_cgroup(self.sandbox_id)
                verified_cgroup = verified.get('cgroup') if isinstance(verified, Mapping) else None
                if self.numa_borrower is not None:
                    with self._measure_stage(stage_seconds, "pin_local_s"):
                        placement = (
                            verified if isinstance(verified, Mapping)
                            and isinstance(verified.get('actual_residency_bytes'), Mapping)
                            and isinstance(verified.get('actual_anon_residency_bytes'), Mapping)
                            else None
                        )
                        if verified_cgroup is None:
                            self.numa_borrower.pin_local(self.sandbox_id)
                        else:
                            self.numa_borrower.pin_local(
                                self.sandbox_id, verified_cgroup=verified_cgroup,
                                **({'verified_placement': placement} if placement else {}),
                            )
                if self._snapshot_key is not None and self._tier is SnapshotTier.WARM:
                    if self.snapshot_pool is None:
                        raise RuntimeError("WARM snapshot accounting disappeared before restore")
                    if self.lazy_restore:
                        self.snapshot_pool.retain_for_vm(self._snapshot_key)
                    else:
                        self._verify_warm_files_removed()
                        self.snapshot_pool.remove(self._snapshot_key, consume_pinned=True)
                if not self.lazy_restore:
                    self._snapshot_key = None
                    self._snapshot_tier = None
                self._tier = SnapshotTier.LOCAL
                self._state = SandboxState.RUNNING
                from pathlib import Path
                from clawbox.experiments.memory import SandboxRSSSampler
                with self._measure_stage(stage_seconds, "restore_fault_observation_s"):
                    try:
                        # Verification already identifies this VM's current
                        # leaf. Avoid a host-wide /proc scan on every restore.
                        self._restore_fault_sampler = (
                            SandboxRSSSampler(self.sandbox_id,
                                cgroup=Path(verified_cgroup) if verified_cgroup is not None else None)
                            if Path("/proc").is_dir() else None
                        )
                    except OSError:
                        self._restore_fault_sampler = None
                elapsed = self._record("restore", before, self._state,
                                    started_wall, started_mono,
                                    host_memory_before=host_memory_before,
                                    stage_seconds=stage_seconds,
                                    tier_from=self._snapshot_tier.value if self._snapshot_tier else None,
                                    tier_to=SnapshotTier.LOCAL.value,
                                    snapshot_generation=self._generation)
                self._first_tool_restore = {
                    "restore_ready_monotonic_s": self._timings[-1].completed_monotonic_s,
                    "restore_ready_service_seconds": elapsed,
                    "snapshot_generation": self._generation,
                    "snapshot_tier": self._snapshot_tier.value if self._snapshot_tier else None,
                }
                return elapsed
            except Exception as exc:
                remote_state = None
                if resume_attempted:
                    try:
                        remote_state = self.client.get_sandbox_state(self.sandbox)
                    except Exception:
                        pass
                self._state = (before if not resume_attempted or remote_state == "paused"
                               else SandboxState.UNKNOWN)
                self._record(
                    "restore", before, self._state, started_wall, started_mono,
                    status="error", error_type=type(exc).__name__,
                    host_memory_before=host_memory_before,
                    stage_seconds=stage_seconds,
                )
                raise

    @contextmanager
    def _transfer(self, operation, stage_seconds=None):
        started = time.monotonic()
        acquired = False
        context = self.snapshot_pool.transfer(operation) if self.snapshot_pool is not None else nullcontext()
        try:
            with context:
                acquired = True
                if stage_seconds is not None:
                    stage_seconds["transfer_slot_wait_s"] = time.monotonic() - started
                yield
        finally:
            if not acquired and stage_seconds is not None:
                stage_seconds["transfer_slot_wait_s"] = time.monotonic() - started

    def _verify_warm_files_removed(self):
        """Do not credit released capacity while an owned WARM file remains."""
        from pathlib import Path
        remaining = [str(file) for path in self._warm_paths
                     for file in (Path(path), Path(path+'.lineage.json'), Path(path+'.metrics.json'),
                                  *Path(path+'.layers').glob('*')) if file.exists()]
        if remaining:
            raise LifecycleError('WARM capacity retained: snapshot files remain after reclamation: '
                                 + ', '.join(remaining) + '; inspect Cube cleanup and retry')
        self._warm_paths.clear()

    def observe_borrow_candidate(self):
        """Keep checkpoint/cleanup from removing the VM during residency reads."""
        with self._lock:
            if self._state in {SandboxState.SWAPPED, SandboxState.CLOSED}:
                return None
            if self._state is not SandboxState.RUNNING or self.sandbox_id is None:
                raise LifecycleError("only a running sandbox has borrow-candidate residency")
            if self.numa_borrower is None:
                raise LifecycleError("shared NUMA borrowing is not configured")
            return self.numa_borrower.observe_resident(self.sandbox_id)

    def borrow_shared(self):
        with self._lock:
            if self._state in {SandboxState.SWAPPED, SandboxState.CLOSED}:
                return None  # a checkpoint/cleanup won the lifecycle lock
            if self._state is not SandboxState.RUNNING or self.sandbox_id is None:
                raise LifecycleError("only a running sandbox can borrow shared memory")
            if self.numa_borrower is None or self.configured_memory_bytes is None:
                raise LifecycleError("shared NUMA borrowing is not configured")
            return self.numa_borrower.borrow(
                self.sandbox_id, self.configured_memory_bytes,
            )

    def quarantine_incomplete_rebind(self) -> bool:
        """Exclude a VM whose kernel migration outcome cannot be verified.

        The borrow reservation remains owned by ``SandboxNumaBorrower`` until
        ordinary session cleanup destroys the VM and verifies that its shared
        pages are gone.  Marking the lifecycle UNKNOWN prevents admission and
        victim selection from using the VM in the meantime.
        """
        with self._lock:
            if self._state is SandboxState.CLOSED:
                return False
            if self._state is not SandboxState.RUNNING:
                return False
            self._state = SandboxState.UNKNOWN
            return True

    def return_local_memory(self):
        with self._lock:
            if self._state in {SandboxState.SWAPPED, SandboxState.CLOSED}:
                return None  # checkpoint/cleanup released the live borrower
            if self._state is not SandboxState.RUNNING or self.sandbox_id is None:
                raise LifecycleError("only a running sandbox can return LOCAL memory")
            if self.numa_borrower is None:
                raise LifecycleError("shared NUMA borrowing is not configured")
            return self.numa_borrower.return_local(self.sandbox_id)

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
                host_memory_before = self._observe_physical()
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
                        host_memory_before=host_memory_before,
                    )
                    raise
                self._record("network_update", before, self._state,
                             started_wall, started_mono,
                             host_memory_before=host_memory_before)
            self.network_allow_out = desired
            return True

    def close(self) -> float:
        with self._snapshot_access():
            if self._state is SandboxState.CLOSED:
                return 0.0
            before = self._state
            self._state = SandboxState.DESTROYING
            started_wall, started_mono = time.time(), time.monotonic()
            host_memory_before = self._observe_physical()
            try:
                if self.sandbox_id is not None:
                    self.client.kill_sandbox(self.sandbox_id)
                    if self.numa_borrower is not None:
                        self.numa_borrower.release_destroyed(self.sandbox_id)
                self._verify_warm_files_removed()
                if (self._snapshot_key is not None and
                        self._snapshot_tier is SnapshotTier.WARM and
                        self.snapshot_pool is not None):
                    self.snapshot_pool.remove(self._snapshot_key, consume_pinned=True)
                if self.snapshot_pool is not None:
                    for key in self._pending_snapshot_keys:
                        self.snapshot_pool.abort(key)
                    self._pending_snapshot_keys.clear()
                self._snapshot_key = None
                self.sandbox = None
                self.sandbox_id = None
                self._state = SandboxState.CLOSED
                return self._record("destroy", before, self._state,
                                    started_wall, started_mono,
                                    host_memory_before=host_memory_before)
            except Exception as exc:
                self._state = before
                self._record(
                    "destroy", before, before, started_wall, started_mono,
                    status="error", error_type=type(exc).__name__,
                    host_memory_before=host_memory_before,
                )
                raise
