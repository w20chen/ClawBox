"""Atomic accounting for the bounded NUMA-backed WARM snapshot pool."""
from __future__ import annotations

import time
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import asdict, dataclass
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


class WarmSnapshotPool:
    """Serialize reservations, manifests and deterministic per-generation LRU.

    Reserved bytes cover writes not represented by a published manifest.
    Committed bytes cover allocated WARM files.  A byte is never present in
    both ledgers, and every mutation checks the configured hard capacity.
    """

    def __init__(self, capacity_bytes: int, *,
                 borrow_capacity_bytes: int = 0,
                 transfer_concurrency: int = 1,
                 transfer_timeout_seconds: float = 120,
                 snapshot_aware: bool = False,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if capacity_bytes < 0:
            raise ValueError("capacity_bytes must be non-negative")
        if borrow_capacity_bytes < 0 or borrow_capacity_bytes > capacity_bytes:
            raise ValueError("borrow_capacity_bytes must be between zero and capacity")
        self.capacity_bytes = capacity_bytes
        self.borrow_capacity_bytes = borrow_capacity_bytes
        if transfer_concurrency < 1 or transfer_timeout_seconds <= 0:
            raise ValueError("transfer concurrency and timeout must be positive")
        self.transfer_concurrency = transfer_concurrency
        self.transfer_timeout_seconds = transfer_timeout_seconds
        self.snapshot_aware = snapshot_aware
        self._transfer_slots = BoundedSemaphore(transfer_concurrency)
        self._transfers: dict[str, int] = {}
        self._clock = clock
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._admission_lock = Lock()
        self._reservations: dict[SnapshotKey, int] = {}
        self._borrow_reservations: dict[str, int] = {}
        self._manifests: dict[SnapshotKey, SnapshotManifest] = {}
        self._spillers: dict[SnapshotKey, Callable[[SnapshotManifest], None]] = {}
        self._warm_admissions = 0
        self._value_rejections = 0
        self._spill_count = 0

    @contextmanager
    def transfer(self, operation: str):
        """Bound concurrent I/O/rebind calls; this is not a bandwidth meter.

        Call only around VMM I/O, after capacity/spill/pin decisions, so a
        waiter never holds a transfer slot while waiting for another transfer.
        """
        if not self._transfer_slots.acquire(timeout=self.transfer_timeout_seconds):
            raise TimeoutError(f"shared-pool {operation} transfer timed out; inspect active transfers or increase resources.pool_transfer_timeout_seconds")
        with self._lock:
            self._transfers[operation] = self._transfers.get(operation, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._transfers[operation] -= 1
            self._transfer_slots.release()

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return sum(self._reservations.values())

    @property
    def committed_bytes(self) -> int:
        with self._lock:
            return sum(item.allocated_bytes for item in self._manifests.values())

    @property
    def borrowed_bytes(self) -> int:
        with self._lock:
            return sum(self._borrow_reservations.values())

    def reserve_borrow(self, sandbox_id: str, upper_bound_bytes: int) -> None:
        """Reserve live VM capacity on the shared NUMA node.

        The configured VM footprint is reserved rather than its current RSS so
        future guest growth cannot silently consume space already promised to
        an in-flight checkpoint.
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
            if self._usage_locked() + upper_bound_bytes > self.capacity_bytes:
                raise WarmCapacityError("shared NUMA capacity exceeded")
            self._borrow_reservations[sandbox_id] = upper_bound_bytes
            self._assert_capacity_locked()
            self._changed.notify_all()

    def release_borrow(self, sandbox_id: str) -> int:
        with self._lock:
            try:
                released = self._borrow_reservations.pop(sandbox_id)
            except KeyError as exc:
                raise RuntimeError(f"unknown shared-memory borrow: {sandbox_id}") from exc
            self._changed.notify_all()
            return released

    def reserve(self, key: SnapshotKey, upper_bound_bytes: int) -> None:
        if upper_bound_bytes <= 0:
            raise ValueError("upper_bound_bytes must be positive")
        with self._lock:
            if upper_bound_bytes > self.capacity_bytes:
                raise WarmSnapshotTooLarge(
                    f"snapshot reservation {upper_bound_bytes} exceeds WARM capacity "
                    f"{self.capacity_bytes}"
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
               retention_value_seconds: float = 0.0) -> SnapshotManifest:
        if logical_bytes < 0 or allocated_bytes < 0 or (
            transferred_bytes is not None and transferred_bytes < 0
        ):
            raise ValueError("snapshot byte counts must be non-negative")
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
            )
            self._manifests[key] = manifest
            if predecessor is not None:
                self._manifests.pop(replaces)
                self._spillers.pop(replaces, None)
            if spiller is not None:
                self._spillers[key] = spiller
            self._assert_capacity_locked()
            self._changed.notify_all()
            return manifest

    def reserve_for_admission(self, key: SnapshotKey, upper_bound_bytes: int,
                              *, timeout_s: float = 120,
                              retention_value_seconds: float = 0.0) -> None:
        """Keep spill-and-reserve atomic against other admissions.

        Writers commit without the admission lock. If capacity is temporarily
        reserved or pinned, wait for their completion instead of failing an
        otherwise valid parallel checkpoint.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                with self._admission_lock:
                    self.spill_for_admission(
                        upper_bound_bytes, exclude={key},
                        candidate_retention_value_seconds=retention_value_seconds,
                    )
                    self.reserve(key, upper_bound_bytes)
                    with self._lock:
                        self._warm_admissions += 1
                return
            except WarmAdmissionRejected:
                with self._lock:
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

    def spill_for_admission(self, required_bytes: int, *,
                            exclude: set[SnapshotKey] | None = None,
                            candidate_retention_value_seconds: float = 0.0,
                            ) -> tuple[SnapshotManifest, ...]:
        """Relocate and retire the deterministic pinned LRU prefix."""
        victims = self.lru_victims(
            required_bytes, exclude=exclude,
            candidate_retention_value_seconds=candidate_retention_value_seconds,
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

    def abort(self, key: SnapshotKey) -> None:
        with self._lock:
            self._reservations.pop(key, None)
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

    def retain_for_vm(self, key: SnapshotKey) -> None:
        """A running lazy restore still references these physical WARM pages."""
        with self._lock:
            self._manifests[key].retained_by_vm = True

    def manifest(self, key: SnapshotKey) -> SnapshotManifest:
        with self._lock:
            return self._manifests[key]

    def remove(self, key: SnapshotKey, *, consume_pinned: bool = False) -> SnapshotManifest:
        with self._lock:
            item = self._manifests[key]
            if item.pinned != (1 if consume_pinned else 0):
                raise RuntimeError(f"cannot remove pinned snapshot: {key}")
            self._spillers.pop(key, None)
            removed = self._manifests.pop(key)
            self._changed.notify_all()
            return removed

    def lru_victims(self, required_bytes: int, *, exclude: set[SnapshotKey] | None = None,
                    candidate_retention_value_seconds: float = 0.0,
                    ) -> tuple[SnapshotManifest, ...]:
        """Return and pin deterministic victims needed for admission.

        Legacy mode is strict LRU. Snapshot-aware mode evicts the lowest
        expected restore-time saving per allocated byte and rejects a lower
        value candidate so its lifecycle can write it directly to SSD.
        """
        if required_bytes <= 0:
            return ()
        if required_bytes > self.capacity_bytes:
            raise WarmSnapshotTooLarge(
                f"snapshot reservation {required_bytes} exceeds WARM capacity "
                f"{self.capacity_bytes}"
            )
        excluded = exclude or set()
        with self._lock:
            deficit = max(0, self._usage_locked() + required_bytes - self.capacity_bytes)
            if deficit == 0:
                return ()
            def victim_order(item: SnapshotManifest):
                stable = (
                    item.last_used_monotonic_s,
                    item.key.session_id, item.key.role, item.key.generation,
                )
                if not self.snapshot_aware:
                    return stable
                density = item.retention_value_seconds / max(1, item.allocated_bytes)
                return (density, *stable)
            candidates = sorted(
                (item for key, item in self._manifests.items()
                 if key not in excluded and item.pinned == 0 and not item.retained_by_vm),
                key=victim_order,
            )
            victims: list[SnapshotManifest] = []
            reclaimed = 0
            for item in candidates:
                item.pinned += 1
                victims.append(item)
                reclaimed += item.allocated_bytes
                if reclaimed >= deficit:
                    break
            if reclaimed < deficit:
                for item in victims:
                    item.pinned -= 1
                raise WarmCapacityError("WARM capacity has no spillable LRU prefix")
            if self.snapshot_aware and candidate_retention_value_seconds <= sum(
                item.retention_value_seconds for item in victims
            ):
                for item in victims:
                    item.pinned -= 1
                raise WarmAdmissionRejected(
                    "candidate has no greater expected restore saving than WARM victims"
                )
            return tuple(victims)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "capacity_bytes": self.capacity_bytes,
                "transfer_concurrency": self.transfer_concurrency,
                "active_transfers": dict(self._transfers),
                "bandwidth_measurement": "unavailable",
                "residency_policy": "value_aware" if self.snapshot_aware else "lru",
                "warm_admissions": self._warm_admissions,
                "value_rejections_to_cold": self._value_rejections,
                "spill_count": self._spill_count,
                "reserved_bytes": sum(self._reservations.values()),
                "committed_bytes": sum(
                    item.allocated_bytes for item in self._manifests.values()
                ),
                "borrow_capacity_bytes": self.borrow_capacity_bytes,
                "borrowed_bytes": sum(self._borrow_reservations.values()),
                "borrow_reservations": dict(sorted(self._borrow_reservations.items())),
                "reservations": {
                    repr(key): value for key, value in self._reservations.items()
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
        return sum(self._borrow_reservations.values()) + sum(self._reservations.values()) + sum(
            item.allocated_bytes for item in self._manifests.values()
        )

    def _assert_capacity_locked(self) -> None:
        usage = self._usage_locked()
        if usage > self.capacity_bytes:
            raise AssertionError(
                f"WARM accounting invariant violated: {usage} > {self.capacity_bytes}"
            )
