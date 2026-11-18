"""Atomic accounting for the bounded NUMA-backed WARM snapshot pool."""
from __future__ import annotations

import time
import os
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from threading import BoundedSemaphore, Condition, Lock, RLock

from .spec_types import SnapshotTier


class WarmCapacityError(RuntimeError):
    """The requested WARM generation cannot currently be admitted."""


class WarmSnapshotTooLarge(WarmCapacityError):
    """One generation is larger than the complete WARM pool."""


class WarmAdmissionRejected(WarmCapacityError):
    """A lower-value candidate should be written directly to the SSD tier."""


@dataclass(frozen=True, slots=True)
class SnapshotKey:
    session_id: str
    role: str
    generation: int


@dataclass(slots=True)
class SnapshotManifest:
    key: SnapshotKey
    tier: SnapshotTier
    path: str
    logical_bytes: int
    allocated_bytes: int
    transferred_bytes: int | None
    last_used_monotonic_s: float
    committed_monotonic_s: float
    pinned: int = 0
    retained_by_vm: bool = False
    template_id: str | None = None
    shared_base_identity: str | None = None
    shared_base_allocated_bytes: int = 0
    private_delta_bytes: int = 0
    dependency_count: int = 0
    expected_reuse_probability: float | None = None
    retention_value_seconds: float = 0.0
    next_use_monotonic_s: float | None = None
    next_use_revision: int = 0
    compressed_allocated_bytes: int = 0
    raw_allocated_bytes: int = 0
    expansion_allocated_bytes: int = 0
    runtime_reserved_bytes: int = 0
    owned_logical_bytes: int = 0
    owned_allocated_bytes: int = 0
    expanded: bool = True
    owned_layer_identities: tuple[str, ...] = ()
    owned_layer_files: dict[str, tuple[str, int]] = field(default_factory=dict)


class WarmSnapshotPool:
    """Serialize reservations, manifests and deterministic per-generation LRU.

    Reserved bytes cover writes not represented by a published manifest.
    Committed bytes cover allocated WARM files.  A byte is never present in
    both ledgers, and every mutation checks the configured hard capacity.
    """

    def __init__(self, capacity_bytes: int, *,
                 borrow_capacity_bytes: int = 0,
                 borrow_resident_headroom_bytes: int | None = None,
                 external_shared_live_bytes: int = 0,
                 external_warm_bytes: int = 0,
                 transfer_concurrency: int = 1,
                 snapshot_transfer_concurrency: int | None = None,
                 transfer_timeout_seconds: float = 120,
                 snapshot_aware: bool = False,
                 next_use_aware: bool = False,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if capacity_bytes < 0:
            raise ValueError("capacity_bytes must be non-negative")
        if borrow_capacity_bytes < 0 or borrow_capacity_bytes > capacity_bytes:
            raise ValueError("borrow_capacity_bytes must be between zero and capacity")
        if (borrow_resident_headroom_bytes is not None
                and not 0 < borrow_resident_headroom_bytes < capacity_bytes):
            raise ValueError(
                "borrow resident headroom must be positive and smaller than capacity"
            )
        if borrow_resident_headroom_bytes is not None and borrow_capacity_bytes == 0:
            raise ValueError("borrow resident headroom requires borrow capacity")
        external_occupied_bytes = external_shared_live_bytes + external_warm_bytes
        if (external_shared_live_bytes < 0 or external_warm_bytes < 0
                or external_occupied_bytes > capacity_bytes):
            raise ValueError("pre-existing shared occupancy must be between zero and capacity")
        self.capacity_bytes = capacity_bytes
        self.borrow_capacity_bytes = borrow_capacity_bytes
        self.borrow_resident_headroom_bytes = borrow_resident_headroom_bytes
        # Measured before this run starts allocating: existing shared-node
        # cache and WARM files are outside its borrow/manifest ledgers. Keep
        # their space unavailable rather than promise it to new generations.
        self.external_occupied_bytes = external_occupied_bytes
        self.external_shared_live_bytes = external_shared_live_bytes
        self.external_warm_bytes = external_warm_bytes
        self._observed_shared_live_bytes = external_shared_live_bytes
        if transfer_concurrency < 1 or transfer_timeout_seconds <= 0:
            raise ValueError("transfer concurrency and timeout must be positive")
        if snapshot_transfer_concurrency is None:
            snapshot_transfer_concurrency = transfer_concurrency
        if not 1 <= snapshot_transfer_concurrency <= transfer_concurrency:
            raise ValueError(
                "snapshot transfer concurrency must be between one and total transfer concurrency"
            )
        self.transfer_concurrency = transfer_concurrency
        self.snapshot_transfer_concurrency = snapshot_transfer_concurrency
        self.transfer_timeout_seconds = transfer_timeout_seconds
        if snapshot_aware and next_use_aware:
            raise ValueError("snapshot value and next-use policies are mutually exclusive")
        self.snapshot_aware = snapshot_aware
        self.next_use_aware = next_use_aware
        self._transfer_slots = BoundedSemaphore(transfer_concurrency)
        # Cube create, checkpoint, restore, spill and NUMA rebind still share
        # one host-wide lifecycle budget.  A smaller snapshot budget prevents
        # a checkpoint wave from occupying every slot needed for creation.
        self._snapshot_transfer_slots = BoundedSemaphore(snapshot_transfer_concurrency)
        # Linux cpuset migrations share one ordered kernel workqueue. Queue
        # rebind transactions here instead of making concurrent cgroup writes
        # drain each other's migrations while holding kernel cgroup locks.
        self.rebind_lock = RLock()
        self._transfers: dict[str, int] = {}
        self._clock = clock
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._admission_lock = Lock()
        self._reservations: dict[SnapshotKey, int] = {}
        self._expansion_reservations: dict[SnapshotKey, int] = {}
        self._borrow_reservations: dict[str, int] = {}
        # A new rebind keeps its complete configured footprint charged until a
        # post-rebind parent-cgroup observation proves its physical residency.
        # Periodic accounting remains one O(1) aggregate update, not one NUMA
        # file read and lock acquisition per borrowed VM.
        self._unobserved_borrows: set[str] = set()
        self._manifests: dict[SnapshotKey, SnapshotManifest] = {}
        # Managed ancestors can remain on tmpfs even when their child is COLD.
        # Native GC owns their lifetime; charge physical inodes until removed.
        self._managed_warm_layers: dict[str, tuple[str, int]] = {}
        self._managed_warm_refresh_at = float('-inf')
        self._spillers: dict[SnapshotKey, Callable[[SnapshotManifest], None]] = {}
        self._warm_admissions = 0
        self._value_rejections = 0
        self._next_use_rejections = 0
        self._spill_count = 0

    @contextmanager
    def transfer(self, operation: str):
        """Bound concurrent Cube lifecycle calls; this is not a bandwidth meter.

        Call only around VMM I/O, after capacity/spill/pin decisions, so a
        waiter never holds a transfer slot while waiting for another transfer.
        """
        deadline = time.monotonic() + self.transfer_timeout_seconds
        snapshot_slot = operation in {"checkpoint", "restore", "spill"}
        if snapshot_slot and not self._snapshot_transfer_slots.acquire(
            timeout=self.transfer_timeout_seconds
        ):
            raise TimeoutError(
                f"Cube lifecycle {operation} snapshot slot timed out; inspect active operations "
                "or increase resources.pool_transfer_timeout_seconds"
            )
        acquired_total = False
        try:
            remaining = max(0.0, deadline - time.monotonic())
            acquired_total = self._transfer_slots.acquire(timeout=remaining)
            if not acquired_total:
                raise TimeoutError(
                    f"Cube lifecycle {operation} slot timed out; inspect active operations "
                    "or increase resources.pool_transfer_timeout_seconds"
                )
        except BaseException:
            if snapshot_slot:
                self._snapshot_transfer_slots.release()
            raise
        with self._lock:
            self._transfers[operation] = self._transfers.get(operation, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._transfers[operation] -= 1
            self._transfer_slots.release()
            if snapshot_slot:
                self._snapshot_transfer_slots.release()

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return sum(self._reservations.values())

    @property
    def expansion_reserved_bytes(self) -> int:
        with self._lock:
            return sum(self._expansion_reservations.values())

    @property
    def committed_bytes(self) -> int:
        with self._lock:
            return self._committed_bytes_locked()

    def _track_managed_warm_layers_locked(self, layers) -> None:
        from clawbox.cube.snapshot import _allocated_bytes
        for layer in layers:
            path = Path(layer['path'])
            stat = path.stat()
            if f'{stat.st_dev}:{stat.st_ino}' != layer['identity']:
                raise RuntimeError(f'managed WARM layer identity changed: {path}')
            self._managed_warm_layers[layer['identity']] = (
                str(path), _allocated_bytes(path, stat),
            )

    def _refresh_owned_layers_locked(self, item: SnapshotManifest) -> None:
        from clawbox.cube.snapshot import _allocated_bytes
        for identity, (path, previous) in list(item.owned_layer_files.items()):
            try:
                stat = os.stat(path)
            except FileNotFoundError:
                continue  # predecessor retirement releases its manifest atomically
            if f'{stat.st_dev}:{stat.st_ino}' != identity:
                raise RuntimeError(f'owned WARM layer identity changed: {path}')
            allocated = _allocated_bytes(Path(path), stat)
            change = allocated - previous
            if change > item.runtime_reserved_bytes:
                raise RuntimeError(
                    f'owned snapshot growth {change} exceeds runtime reserve '
                    f'{item.runtime_reserved_bytes}: {item.key}'
                )
            item.allocated_bytes += change
            item.raw_allocated_bytes += change
            if not item.expanded:
                item.compressed_allocated_bytes += change
            if change > 0:
                item.runtime_reserved_bytes -= change
            item.owned_allocated_bytes = max(0, item.owned_allocated_bytes + change)
            item.owned_layer_files[identity] = (path, allocated)

    def refresh_managed_warm_layers(self, *, force: bool = True) -> None:
        """Release charges only after native GC has actually removed a layer."""
        with self._lock:
            now = self._clock()
            if not force and now - self._managed_warm_refresh_at < 1.0:
                return
            for item in self._manifests.values():
                self._refresh_owned_layers_locked(item)
            for identity, (path, _) in list(self._managed_warm_layers.items()):
                try:
                    stat = os.stat(path)
                except FileNotFoundError:
                    del self._managed_warm_layers[identity]
                    continue
                if f'{stat.st_dev}:{stat.st_ino}' != identity:
                    raise RuntimeError(f'managed WARM layer identity changed: {path}')
                from clawbox.cube.snapshot import _allocated_bytes
                self._managed_warm_layers[identity] = (path, _allocated_bytes(Path(path), stat))
            self._managed_warm_refresh_at = now
            self._changed.notify_all()

    def _managed_warm_bytes_locked(self) -> int:
        owned = {identity for item in self._manifests.values()
                 for identity in item.owned_layer_identities}
        return sum(size for identity, (_, size) in self._managed_warm_layers.items()
                   if identity not in owned)

    def _committed_bytes_locked(self) -> int:
        total = sum(item.allocated_bytes for item in self._manifests.values())
        seen = set()
        for item in self._manifests.values():
            for identity, (_, allocated) in item.owned_layer_files.items():
                if identity in seen:
                    total -= allocated
                seen.add(identity)
        return total + self._managed_warm_bytes_locked()

    def commit_cold_dependencies(self, layers, *, replaces: SnapshotKey | None = None) -> None:
        """Atomically hand a retired WARM generation's charge to its ancestors."""
        with self._lock:
            if replaces is not None:
                item = self._manifests[replaces]
                if item.pinned:
                    raise RuntimeError(f'cannot replace pinned snapshot: {replaces}')
            self._track_managed_warm_layers_locked(layers)
            if replaces is not None:
                self.remove(replaces)
            self.refresh_managed_warm_layers(force=False)
            self._assert_capacity_locked()

    @property
    def borrowed_bytes(self) -> int:
        """Configured upper bounds retained for identity and borrow-limit checks."""
        with self._lock:
            return sum(self._borrow_reservations.values())

    @property
    def accounted_bytes(self) -> int:
        with self._lock:
            return self._usage_locked()

    @property
    def borrow_headroom_bytes(self) -> int:
        """Current reservation headroom; reserve_borrow still commits atomically."""
        with self._lock:
            first_borrow_headroom = (
                self.borrow_resident_headroom_bytes
                if (self.borrow_resident_headroom_bytes is not None
                    and not self._borrow_reservations)
                else 0
            )
            return max(0, min(
                self.borrow_capacity_bytes - sum(self._borrow_reservations.values()),
                self.capacity_bytes - self._usage_locked() - first_borrow_headroom,
            ))

    def reserve_borrow(self, sandbox_id: str, upper_bound_bytes: int) -> None:
        """Reserve live VM capacity on the shared NUMA node.

        The configured VM footprint remains the borrow-limit charge. In
        resident mode it is also a temporary pool charge until the completed
        rebind is covered by a parent-cgroup residency observation.
        """
        if not sandbox_id:
            raise ValueError("sandbox_id must be non-empty")
        if upper_bound_bytes <= 0:
            raise ValueError("upper_bound_bytes must be positive")
        with self._lock:
            if sandbox_id in self._borrow_reservations:
                raise RuntimeError(f"duplicate shared-memory borrow: {sandbox_id}")
            if self.borrowed_bytes + upper_bound_bytes > self.borrow_capacity_bytes:
                raise WarmCapacityError("shared live-borrow capacity exceeded")
            first_borrow_headroom = (
                self.borrow_resident_headroom_bytes
                if (self.borrow_resident_headroom_bytes is not None
                    and not self._borrow_reservations)
                else 0
            )
            if (self._usage_locked() + upper_bound_bytes + first_borrow_headroom
                    > self.capacity_bytes):
                raise WarmCapacityError("shared NUMA capacity exceeded")
            self._borrow_reservations[sandbox_id] = upper_bound_bytes
            self._unobserved_borrows.add(sandbox_id)
            self._assert_capacity_locked()
            self._changed.notify_all()

    def release_borrow(self, sandbox_id: str) -> int:
        with self._lock:
            try:
                released = self._borrow_reservations.pop(sandbox_id)
            except KeyError as exc:
                raise RuntimeError(f"unknown shared-memory borrow: {sandbox_id}") from exc
            self._unobserved_borrows.discard(sandbox_id)
            self._changed.notify_all()
            return released

    def observe_shared_live(self, shared_live_bytes: int, *,
                            settle_borrow: str | None = None) -> int:
        """Publish one aggregate NUMA sample and return accounting overage.

        Growth is external to this lock, so an overage is a pressure signal for
        proactive WARM-to-COLD spill rather than an accounting assertion.
        """
        if shared_live_bytes < 0:
            raise ValueError("shared_live_bytes must be non-negative")
        with self._lock:
            self._observed_shared_live_bytes = shared_live_bytes
            if settle_borrow is not None:
                self._unobserved_borrows.discard(settle_borrow)
            excess = max(0, self._usage_locked() - self.capacity_bytes)
            self._changed.notify_all()
            return excess

    def reserve(self, key: SnapshotKey, upper_bound_bytes: int) -> None:
        if upper_bound_bytes <= 0:
            raise ValueError("upper_bound_bytes must be positive")
        with self._lock:
            if upper_bound_bytes > self.capacity_bytes - self.external_occupied_bytes:
                raise WarmSnapshotTooLarge(
                    f"snapshot reservation {upper_bound_bytes} exceeds WARM capacity "
                    f"{self.capacity_bytes - self.external_occupied_bytes} remaining "
                    "after pre-existing shared occupancy"
                )
            if key in self._reservations or key in self._manifests:
                raise RuntimeError(f"duplicate snapshot generation: {key}")
            if self._usage_locked() + upper_bound_bytes > self.capacity_bytes:
                raise WarmCapacityError("WARM capacity requires spill")
            self._reservations[key] = upper_bound_bytes
            self._assert_capacity_locked()

    def commit(self, key: SnapshotKey, *, path: str, logical_bytes: int,
               allocated_bytes: int, transferred_bytes: int | None,
               spiller: Callable[[SnapshotManifest], None] | None = None,
               replaces: SnapshotKey | None = None,
               template_id: str | None = None,
               shared_base_identity: str | None = None,
               shared_base_allocated_bytes: int = 0,
               private_delta_bytes: int = 0,
               dependency_count: int = 0,
               expected_reuse_probability: float | None = None,
               retention_value_seconds: float = 0.0,
               next_use_monotonic_s: float | None = None,
               next_use_revision: int = 0,
               compressed_allocated_bytes: int = 0,
               raw_allocated_bytes: int | None = None,
               expansion_allocated_bytes: int = 0,
               owned_logical_bytes: int = 0,
               owned_allocated_bytes: int = 0,
               expanded: bool | None = None,
               owned_layer_identities: tuple[str, ...] = (),
               owned_layer_files: dict[str, tuple[str, int]] | None = None,
               managed_warm_layers: tuple[dict, ...] = ()) -> SnapshotManifest:
        if logical_bytes < 0 or allocated_bytes < 0 or (
            transferred_bytes is not None and transferred_bytes < 0
        ):
            raise ValueError("snapshot byte counts must be non-negative")
        if compressed_allocated_bytes < 0 or expansion_allocated_bytes < 0:
            raise ValueError("compression byte counts must be non-negative")
        if raw_allocated_bytes is None:
            raw_allocated_bytes = allocated_bytes
        if raw_allocated_bytes < 0:
            raise ValueError("raw snapshot allocation must be non-negative")
        if expanded is None:
            expanded = compressed_allocated_bytes == 0
        with self._lock:
            predecessor = self._manifests.get(replaces) if replaces is not None else None
            if replaces is not None and (
                predecessor is None or not predecessor.retained_by_vm or predecessor.pinned
                or (replaces.session_id, replaces.role) != (key.session_id, key.role)
                or replaces.generation >= key.generation
            ):
                raise RuntimeError("invalid retained snapshot predecessor")
            reserved = self._reservations.pop(key, None)
            if reserved is None:
                raise RuntimeError(f"snapshot generation is not reserved: {key}")
            inherited_bytes = predecessor.allocated_bytes if predecessor is not None else 0
            if allocated_bytes > reserved + inherited_bytes:
                self._reservations[key] = reserved
                raise RuntimeError(
                    f"allocated WARM bytes {allocated_bytes} exceed reservation {reserved}"
                )
            now = self._clock()
            manifest = SnapshotManifest(
                key=key, tier=SnapshotTier.WARM, path=path,
                logical_bytes=logical_bytes, allocated_bytes=allocated_bytes,
                transferred_bytes=transferred_bytes,
                last_used_monotonic_s=now, committed_monotonic_s=now,
                template_id=template_id,
                shared_base_identity=shared_base_identity,
                shared_base_allocated_bytes=shared_base_allocated_bytes,
                private_delta_bytes=private_delta_bytes,
                dependency_count=dependency_count,
                expected_reuse_probability=expected_reuse_probability,
                retention_value_seconds=retention_value_seconds,
                next_use_monotonic_s=next_use_monotonic_s,
                next_use_revision=next_use_revision,
                compressed_allocated_bytes=compressed_allocated_bytes,
                raw_allocated_bytes=raw_allocated_bytes,
                expansion_allocated_bytes=expansion_allocated_bytes,
                owned_logical_bytes=owned_logical_bytes,
                owned_allocated_bytes=owned_allocated_bytes,
                expanded=expanded,
                owned_layer_identities=owned_layer_identities,
                owned_layer_files=dict(owned_layer_files or {}),
            )
            self._manifests[key] = manifest
            if predecessor is not None:
                self._manifests.pop(replaces)
                self._spillers.pop(replaces, None)
            if spiller is not None:
                self._spillers[key] = spiller
            self._track_managed_warm_layers_locked(managed_warm_layers)
            self._refresh_owned_layers_locked(manifest)
            self.refresh_managed_warm_layers(force=False)
            self._assert_capacity_locked()
            self._changed.notify_all()
            return manifest

    def reserve_expansion_for_restore(
        self, key: SnapshotKey, upper_bound_bytes: int, *,
        timeout_s: float = 120,
    ) -> int:
        """Reserve raw expansion space and spill unrelated snapshots if needed.

        The compressed manifest remains charged while its raw files are being
        materialized. The reservation covers the additional peak allocation.
        Callers must pin ``key`` with ``consume_guard`` first so the target
        cannot be selected as a spill victim.
        """
        if upper_bound_bytes <= 0:
            return 0
        deadline = time.monotonic() + timeout_s
        while True:
            with self._lock:
                item = self._manifests.get(key)
                if item is None:
                    raise RuntimeError(f"snapshot generation is not committed: {key}")
                if item.pinned <= 0:
                    raise RuntimeError(
                        f"snapshot expansion requires a consumer pin: {key}"
                    )
                existing = self._expansion_reservations.get(key, 0)
            additional = max(0, upper_bound_bytes - existing)
            try:
                # Pin victims under the pool lock, then perform relocation
                # without the admission lock. A spiller acquires its own VM
                # lifecycle lock; holding admission here would invert the
                # lifecycle->admission order used by checkpoint writers.
                spill = self.spill_for_admission(
                    additional, exclude={key},
                    candidate_retention_value_seconds=float("inf"),
                    enforce_admission_policy=False,
                )
                with self._admission_lock:
                    with self._lock:
                        item = self._manifests.get(key)
                        if item is None:
                            raise RuntimeError(f"snapshot generation disappeared: {key}")
                        existing = self._expansion_reservations.get(key, 0)
                        additional = max(0, upper_bound_bytes - existing)
                        if self._usage_locked() + additional > self.capacity_bytes:
                            raise WarmCapacityError(
                                "WARM expansion requires capacity that cannot be reclaimed"
                            )
                        self._expansion_reservations[key] = existing + additional
                        self._assert_capacity_locked()
                        self._changed.notify_all()
                        return len(spill)
            except WarmSnapshotTooLarge:
                raise
            except WarmCapacityError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                with self._changed:
                    self._changed.wait(timeout=min(remaining, 0.2))

    def reserve_selected_expansion(self, key: SnapshotKey, upper_bound_bytes: int) -> None:
        """Reserve expansion for a victim already selected under admission lock."""
        if upper_bound_bytes <= 0:
            return
        with self._lock:
            if key not in self._manifests:
                raise RuntimeError(f"snapshot generation is not committed: {key}")
            if self._manifests[key].pinned <= 0:
                raise RuntimeError(
                    f"snapshot spill expansion requires a spill pin: {key}"
                )
            existing = self._expansion_reservations.get(key, 0)
            additional = max(0, upper_bound_bytes - existing)
            if self._usage_locked() + additional > self.capacity_bytes:
                raise WarmCapacityError(
                    "compressed spill cannot expand without exceeding WARM capacity"
                )
            self._expansion_reservations[key] = existing + additional
            self._changed.notify_all()

    def finish_expansion(
        self, key: SnapshotKey, raw_allocated_bytes: int, *,
        runtime_upper_bound_bytes: int = 0,
        owned_allocated_bytes: int = 0,
        retained_by_vm: bool = False,
        owned_layer_files: dict[str, tuple[str, int]] | None = None,
    ) -> SnapshotManifest:
        """Replace expansion reservation and compressed charge with raw bytes."""
        if raw_allocated_bytes < 0:
            raise ValueError("raw snapshot allocation must be non-negative")
        with self._lock:
            item = self._manifests[key]
            reserved = self._expansion_reservations.pop(key, None)
            if reserved is None:
                if item.expanded and item.allocated_bytes == raw_allocated_bytes:
                    if retained_by_vm:
                        item.retained_by_vm = True
                    return item
                raise RuntimeError(f"snapshot expansion was not reserved: {key}")
            runtime_reserved = max(
                0, runtime_upper_bound_bytes - owned_allocated_bytes
            )
            if raw_allocated_bytes + runtime_reserved > item.allocated_bytes + reserved:
                self._expansion_reservations[key] = reserved
                raise RuntimeError(
                    f"raw expansion {raw_allocated_bytes} plus runtime reserve "
                    f"{runtime_reserved} exceeds reserved peak "
                    f"{item.allocated_bytes + reserved}"
                )
            item.allocated_bytes = raw_allocated_bytes
            item.raw_allocated_bytes = raw_allocated_bytes
            item.compressed_allocated_bytes = 0
            item.expansion_allocated_bytes = 0
            item.runtime_reserved_bytes = runtime_reserved
            item.expanded = True
            item.retained_by_vm = retained_by_vm
            if owned_layer_files is not None:
                item.owned_layer_files = dict(owned_layer_files)
                item.owned_layer_identities = tuple(owned_layer_files)
            self._assert_capacity_locked()
            self._changed.notify_all()
            return item

    def validate_expanded(self, key: SnapshotKey, raw_allocated_bytes: int) -> None:
        """Validate that an already-materialized snapshot matches its charge."""
        with self._lock:
            item = self._manifests[key]
            if not item.expanded or item.compressed_allocated_bytes != 0:
                raise RuntimeError(f"snapshot expansion is still incomplete: {key}")
            if item.expansion_allocated_bytes != 0:
                raise RuntimeError(f"snapshot expansion reservation did not converge: {key}")
            self._refresh_owned_layers_locked(item)
            if raw_allocated_bytes < item.allocated_bytes:
                raise RuntimeError(
                    f"expanded snapshot allocation changed for {key}: "
                    f"charged={item.allocated_bytes} actual={raw_allocated_bytes}"
                )
            growth = raw_allocated_bytes - item.allocated_bytes
            if growth > item.runtime_reserved_bytes:
                raise RuntimeError(
                    f"expanded snapshot growth {growth} exceeds runtime reserve "
                    f"{item.runtime_reserved_bytes}: {key}"
                )
            item.allocated_bytes = raw_allocated_bytes
            item.raw_allocated_bytes = raw_allocated_bytes
            item.runtime_reserved_bytes -= growth

    def reserve_for_admission(self, key: SnapshotKey, upper_bound_bytes: int,
                              *, timeout_s: float = 120,
                              retention_value_seconds: float = 0.0,
                              next_use_monotonic_s: float | None = None) -> None:
        """Spill eligible state, then atomically reserve against other writers.

        Slow relocation intentionally runs without the admission lock. The
        final reserve rechecks capacity; temporary reservations or victim pins
        are retried up to ``timeout_s``.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                # Slow relocation runs after victims are pinned but without
                # the admission lock. This avoids admission->lifecycle versus
                # lifecycle->admission deadlocks; the final reserve remains
                # atomic and retries if another writer used the freed bytes.
                self.spill_for_admission(
                    upper_bound_bytes, exclude={key},
                    candidate_retention_value_seconds=retention_value_seconds,
                    candidate_next_use_monotonic_s=next_use_monotonic_s,
                )
                with self._admission_lock:
                    self.reserve(key, upper_bound_bytes)
                    with self._lock:
                        self._warm_admissions += 1
                return
            except WarmAdmissionRejected:
                with self._lock:
                    if self.next_use_aware:
                        self._next_use_rejections += 1
                    else:
                        self._value_rejections += 1
                raise
            except WarmSnapshotTooLarge:
                raise
            except WarmCapacityError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                with self._changed:
                    self._changed.wait(timeout=min(remaining, 0.2))

    def reserve_without_spill(self, key: SnapshotKey, upper_bound_bytes: int) -> None:
        """Atomically reserve currently free WARM space without evicting anything."""
        with self._admission_lock:
            self.reserve(key, upper_bound_bytes)
            with self._lock:
                self._warm_admissions += 1

    def spill_for_admission(self, required_bytes: int, *,
                            exclude: set[SnapshotKey] | None = None,
                            candidate_retention_value_seconds: float = 0.0,
                            candidate_next_use_monotonic_s: float | None = None,
                            enforce_admission_policy: bool = True,
                            ) -> tuple[SnapshotManifest, ...]:
        """Relocate and retire the deterministic pinned LRU prefix."""
        victims = self.lru_victims(
            required_bytes, exclude=exclude,
            candidate_retention_value_seconds=candidate_retention_value_seconds,
            candidate_next_use_monotonic_s=candidate_next_use_monotonic_s,
            enforce_admission_policy=enforce_admission_policy,
        )
        completed: list[SnapshotManifest] = []
        try:
            for item in victims:
                with self._lock:
                    spiller = self._spillers.get(item.key)
                if spiller is None:
                    raise WarmCapacityError(f"WARM snapshot has no spill path: {item.key}")
                spiller(item)
                with self._lock:
                    current = self._manifests.get(item.key)
                    if current is not item or current.pinned != 1:
                        raise RuntimeError(f"WARM spill identity changed: {item.key}")
                    self._manifests.pop(item.key)
                    self._spillers.pop(item.key, None)
                    completed.append(item)
                    self._spill_count += 1
                    self._assert_capacity_locked()
                    self._changed.notify_all()
            return tuple(completed)
        finally:
            with self._lock:
                for item in victims:
                    if item.key in self._manifests and item.pinned > 0:
                        item.pinned -= 1
                self._changed.notify_all()

    def spill_observed_excess(self) -> tuple[SnapshotManifest, ...]:
        """Restore configured headroom after aggregate shared-live growth."""
        # This is a physical safety action, not a value comparison against a
        # new candidate. Any spillable generation may move to COLD.
        return self.spill_for_admission(
            0, candidate_retention_value_seconds=float("inf"),
            enforce_admission_policy=False,
        )

    def abort(self, key: SnapshotKey) -> None:
        with self._lock:
            self._reservations.pop(key, None)
            self._expansion_reservations.pop(key, None)
            self._changed.notify_all()

    def pin(self, key: SnapshotKey) -> SnapshotManifest:
        with self._lock:
            item = self._manifests[key]
            item.pinned += 1
            return item

    def unpin(self, key: SnapshotKey) -> None:
        with self._lock:
            item = self._manifests[key]
            if item.pinned <= 0:
                raise RuntimeError(f"snapshot pin underflow: {key}")
            item.pinned -= 1
            self._changed.notify_all()

    @contextmanager
    def consume_guard(self, key: SnapshotKey):
        """Exclude spill while restoring/deleting a generation.

        Wait without holding a lifecycle lock: an already selected spiller
        needs that lock to finish relocation. A missing manifest means that
        spill completed and the lifecycle now owns a COLD snapshot.
        """
        with self._changed:
            self._changed.wait_for(
                lambda: key not in self._manifests or self._manifests[key].pinned == 0
            )
            item = self._manifests.get(key)
            if item is not None:
                item.pinned += 1
        try:
            yield item
        finally:
            with self._changed:
                if item is not None and self._manifests.get(key) is item:
                    item.pinned -= 1
                self._changed.notify_all()

    def touch(self, key: SnapshotKey) -> None:
        with self._lock:
            self._manifests[key].last_used_monotonic_s = self._clock()

    def update_next_use(
        self, key: SnapshotKey, next_use_monotonic_s: float | None,
        revision: int = 0,
    ) -> None:
        with self._lock:
            item = self._manifests.get(key)
            if item is not None and revision >= item.next_use_revision:
                item.next_use_monotonic_s = next_use_monotonic_s
                item.next_use_revision = revision

    def retain_for_vm(self, key: SnapshotKey) -> None:
        """A running lazy restore still references these physical WARM pages."""
        with self._lock:
            self._manifests[key].retained_by_vm = True

    def manifest(self, key: SnapshotKey) -> SnapshotManifest:
        with self._lock:
            return self._manifests[key]

    def rebind_path(self, key: SnapshotKey, *, expected_path: str, path: str) -> None:
        """Move a pinned generation's naming ownership without charging its inode twice."""
        with self._lock:
            item = self._manifests[key]
            if item.path != expected_path or item.pinned < 1:
                raise RuntimeError("WARM path handoff requires the current pinned generation")
            item.path = path

    def remove(self, key: SnapshotKey, *, consume_pinned: bool = False) -> SnapshotManifest:
        with self._lock:
            item = self._manifests[key]
            if item.pinned != (1 if consume_pinned else 0):
                raise RuntimeError(f"cannot remove pinned snapshot: {key}")
            self._spillers.pop(key, None)
            self._expansion_reservations.pop(key, None)
            removed = self._manifests.pop(key)
            self._changed.notify_all()
            return removed

    def lru_victims(self, required_bytes: int, *, exclude: set[SnapshotKey] | None = None,
                    candidate_retention_value_seconds: float = 0.0,
                    candidate_next_use_monotonic_s: float | None = None,
                    enforce_admission_policy: bool = True,
                    ) -> tuple[SnapshotManifest, ...]:
        """Return and pin deterministic victims needed for admission.

        Legacy mode is strict LRU. Value-aware mode evicts the lowest expected
        restore-time saving per allocated byte. Next-use mode evicts only
        snapshots whose predicted next use is strictly later than the new
        candidate, in latest-use-first order. Either policy can reject a new
        candidate so its lifecycle writes it directly to SSD.
        """
        if required_bytes < 0:
            raise ValueError("required_bytes must be non-negative")
        if required_bytes == 0:
            with self._lock:
                if self._usage_locked() <= self.capacity_bytes:
                    return ()
        if required_bytes > self.capacity_bytes:
            raise WarmSnapshotTooLarge(
                f"snapshot reservation {required_bytes} exceeds WARM capacity"
            )
        if (required_bytes > 0
                and required_bytes > self.capacity_bytes - self.external_occupied_bytes):
            raise WarmSnapshotTooLarge(
                f"snapshot reservation {required_bytes} exceeds WARM capacity "
                f"{self.capacity_bytes - self.external_occupied_bytes} remaining "
                "after pre-existing shared occupancy"
            )
        excluded = exclude or set()
        with self._lock:
            deficit = max(0, self._usage_locked() + required_bytes - self.capacity_bytes)
            if deficit == 0:
                return ()
            compare_next_use = self.next_use_aware and enforce_admission_policy
            if compare_next_use and candidate_next_use_monotonic_s is None:
                raise WarmAdmissionRejected(
                    "candidate has no valid queued.est_wait_s next-use estimate"
                )
            def victim_order(item: SnapshotManifest):
                stable = (
                    item.last_used_monotonic_s,
                    item.key.session_id, item.key.role, item.key.generation,
                )
                if compare_next_use:
                    return (-item.next_use_monotonic_s, *stable)
                if not self.snapshot_aware:
                    return stable
                density = item.retention_value_seconds / max(1, item.allocated_bytes)
                return (density, *stable)
            candidates = sorted(
                (item for key, item in self._manifests.items()
                 if (key not in excluded and key not in self._expansion_reservations
                     and item.pinned == 0 and not item.retained_by_vm
                     and (not compare_next_use or (
                         item.next_use_monotonic_s is not None
                         and item.next_use_monotonic_s > candidate_next_use_monotonic_s
                     )))),
                key=victim_order,
            )
            victims: list[SnapshotManifest] = []
            reclaimed = 0
            remaining_owners: dict[str, int] = {}
            for item in self._manifests.values():
                for identity in item.owned_layer_identities:
                    remaining_owners[identity] = remaining_owners.get(identity, 0) + 1
            for item in candidates:
                item.pinned += 1
                victims.append(item)
                retained = 0
                for identity in item.owned_layer_identities:
                    remaining_owners[identity] -= 1
                    if identity in self._managed_warm_layers:
                        retained += self._managed_warm_layers[identity][1]
                    elif remaining_owners[identity] and identity in item.owned_layer_files:
                        retained += item.owned_layer_files[identity][1]
                reclaimed += max(0, item.allocated_bytes - retained)
                if reclaimed >= deficit:
                    break
            if reclaimed < deficit:
                for item in victims:
                    item.pinned -= 1
                if compare_next_use:
                    raise WarmAdmissionRejected(
                        "no strictly later-use WARM snapshots can cover the admission deficit"
                    )
                raise WarmCapacityError("WARM capacity has no spillable LRU prefix")
            if (self.snapshot_aware and enforce_admission_policy
                    and candidate_retention_value_seconds <= sum(
                item.retention_value_seconds for item in victims
            )):
                for item in victims:
                    item.pinned -= 1
                raise WarmAdmissionRejected(
                    "candidate has no greater expected restore saving than WARM victims"
                )
            return tuple(victims)

    def usage_snapshot(self) -> dict[str, int]:
        """Totals for periodic evidence without serializing stored manifests."""
        with self._lock:
            self.refresh_managed_warm_layers(force=False)
            return {
                "reserved_bytes": sum(self._reservations.values()),
                "expansion_reserved_bytes": sum(
                    self._expansion_reservations.values()
                ),
                "committed_bytes": self._committed_bytes_locked(),
                "managed_warm_bytes": self._managed_warm_bytes_locked(),
                "borrowed_bytes": sum(self._borrow_reservations.values()),
                "borrow_accounted_bytes": self._borrow_charge_locked(),
                "accounted_bytes": self._usage_locked(),
                "observed_shared_live_bytes": self._observed_shared_live_bytes,
                "external_warm_bytes": self.external_warm_bytes,
                "external_shared_live_bytes": self.external_shared_live_bytes,
                "external_occupied_bytes": self.external_occupied_bytes,
            }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            self.refresh_managed_warm_layers(force=False)
            return {
                "capacity_bytes": self.capacity_bytes,
                "external_occupied_bytes": self.external_occupied_bytes,
                "external_shared_live_bytes": self.external_shared_live_bytes,
                "external_warm_bytes": self.external_warm_bytes,
                "transfer_concurrency": self.transfer_concurrency,
                "snapshot_transfer_concurrency": self.snapshot_transfer_concurrency,
                "active_transfers": dict(self._transfers),
                "bandwidth_measurement": "unavailable",
                "residency_policy": (
                    "next_use" if self.next_use_aware
                    else "value_aware" if self.snapshot_aware else "lru"
                ),
                "warm_admissions": self._warm_admissions,
                "value_rejections_to_cold": self._value_rejections,
                "next_use_rejections_to_cold": self._next_use_rejections,
                "spill_count": self._spill_count,
                "reserved_bytes": sum(self._reservations.values()),
                "expansion_reserved_bytes": sum(
                    self._expansion_reservations.values()
                ),
                "committed_bytes": self._committed_bytes_locked(),
                "managed_warm_bytes": self._managed_warm_bytes_locked(),
                "borrow_capacity_bytes": self.borrow_capacity_bytes,
                "borrow_resident_headroom_bytes": self.borrow_resident_headroom_bytes,
                "borrowed_bytes": sum(self._borrow_reservations.values()),
                "borrow_accounted_bytes": self._borrow_charge_locked(),
                "accounted_bytes": self._usage_locked(),
                "observed_shared_live_bytes": self._observed_shared_live_bytes,
                "unobserved_borrows": sorted(self._unobserved_borrows),
                "borrow_reservations": dict(sorted(self._borrow_reservations.items())),
                "reservations": {
                    repr(key): value for key, value in self._reservations.items()
                },
                "expansion_reservations": {
                    repr(key): value
                    for key, value in self._expansion_reservations.items()
                },
                "manifests": [
                    {**asdict(item), "tier": item.tier.value}
                    for item in sorted(
                        self._manifests.values(),
                        key=lambda value: (
                            value.key.session_id, value.key.role, value.key.generation,
                        ),
                    )
                ],
                "shared_base_references": {
                    identity: sum(
                        item.shared_base_identity == identity
                        for item in self._manifests.values()
                    )
                    for identity in sorted({
                        item.shared_base_identity for item in self._manifests.values()
                        if item.shared_base_identity is not None
                    })
                },
            }

    def _usage_locked(self) -> int:
        return self.external_warm_bytes + self._borrow_charge_locked() + sum(self._reservations.values()) + sum(
            self._expansion_reservations.values()
        ) + self._committed_bytes_locked() + sum(
            getattr(item, "runtime_reserved_bytes", 0)
            for item in self._manifests.values()
        )

    def _borrow_charge_locked(self) -> int:
        configured = sum(self._borrow_reservations.values())
        if self.borrow_resident_headroom_bytes is None:
            return self.external_shared_live_bytes + configured
        observed = max(
            self.external_shared_live_bytes, self._observed_shared_live_bytes,
        )
        pending = sum(
            self._borrow_reservations[sandbox_id]
            for sandbox_id in self._unobserved_borrows
            if sandbox_id in self._borrow_reservations
        )
        growth_headroom = (
            self.borrow_resident_headroom_bytes if self._borrow_reservations else 0
        )
        return observed + growth_headroom + pending

    def _assert_capacity_locked(self) -> None:
        usage = self._usage_locked()
        if usage > self.capacity_bytes and self.borrow_resident_headroom_bytes is None:
            raise AssertionError(
                f"WARM accounting invariant violated: {usage} > {self.capacity_bytes}"
            )
