"""Atomic accounting for the bounded NUMA-backed WARM snapshot pool."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from threading import RLock

from .spec_types import SnapshotTier


class WarmCapacityError(RuntimeError):
    """The requested WARM generation cannot currently be admitted."""


class WarmSnapshotTooLarge(WarmCapacityError):
    """One generation is larger than the complete WARM pool."""


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
    transferred_bytes: int
    last_used_monotonic_s: float
    committed_monotonic_s: float
    pinned: int = 0


class WarmSnapshotPool:
    """Serialize reservations, manifests and deterministic per-generation LRU.

    Reserved bytes cover writes not represented by a published manifest.
    Committed bytes cover allocated WARM files.  A byte is never present in
    both ledgers, and every mutation checks the configured hard capacity.
    """

    def __init__(self, capacity_bytes: int) -> None:
        if capacity_bytes < 0:
            raise ValueError("capacity_bytes must be non-negative")
        self.capacity_bytes = capacity_bytes
        self._lock = RLock()
        self._reservations: dict[SnapshotKey, int] = {}
        self._manifests: dict[SnapshotKey, SnapshotManifest] = {}

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return sum(self._reservations.values())

    @property
    def committed_bytes(self) -> int:
        with self._lock:
            return sum(item.allocated_bytes for item in self._manifests.values())

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
               allocated_bytes: int, transferred_bytes: int) -> SnapshotManifest:
        if min(logical_bytes, allocated_bytes, transferred_bytes) < 0:
            raise ValueError("snapshot byte counts must be non-negative")
        with self._lock:
            reserved = self._reservations.pop(key, None)
            if reserved is None:
                raise RuntimeError(f"snapshot generation is not reserved: {key}")
            if allocated_bytes > reserved:
                self._reservations[key] = reserved
                raise RuntimeError(
                    f"allocated WARM bytes {allocated_bytes} exceed reservation {reserved}"
                )
            now = time.monotonic()
            manifest = SnapshotManifest(
                key=key, tier=SnapshotTier.WARM, path=path,
                logical_bytes=logical_bytes, allocated_bytes=allocated_bytes,
                transferred_bytes=transferred_bytes,
                last_used_monotonic_s=now, committed_monotonic_s=now,
            )
            self._manifests[key] = manifest
            self._assert_capacity_locked()
            return manifest

    def abort(self, key: SnapshotKey) -> None:
        with self._lock:
            self._reservations.pop(key, None)

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

    def touch(self, key: SnapshotKey) -> None:
        with self._lock:
            self._manifests[key].last_used_monotonic_s = time.monotonic()

    def remove(self, key: SnapshotKey) -> SnapshotManifest:
        with self._lock:
            item = self._manifests[key]
            if item.pinned:
                raise RuntimeError(f"cannot remove pinned snapshot: {key}")
            return self._manifests.pop(key)

    def lru_victims(self, required_bytes: int, *, exclude: set[SnapshotKey] | None = None
                    ) -> tuple[SnapshotManifest, ...]:
        """Return and pin the deterministic LRU prefix needed for admission."""
        if required_bytes <= 0:
            return ()
        excluded = exclude or set()
        with self._lock:
            deficit = max(0, self._usage_locked() + required_bytes - self.capacity_bytes)
            if deficit == 0:
                return ()
            candidates = sorted(
                (item for key, item in self._manifests.items()
                 if key not in excluded and item.pinned == 0),
                key=lambda item: (
                    item.last_used_monotonic_s,
                    item.key.session_id, item.key.role, item.key.generation,
                ),
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
            return tuple(victims)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "capacity_bytes": self.capacity_bytes,
                "reserved_bytes": sum(self._reservations.values()),
                "committed_bytes": sum(
                    item.allocated_bytes for item in self._manifests.values()
                ),
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
            }

    def _usage_locked(self) -> int:
        return sum(self._reservations.values()) + sum(
            item.allocated_bytes for item in self._manifests.values()
        )

    def _assert_capacity_locked(self) -> None:
        usage = self._usage_locked()
        if usage > self.capacity_bytes:
            raise AssertionError(
                f"WARM accounting invariant violated: {usage} > {self.capacity_bytes}"
            )
