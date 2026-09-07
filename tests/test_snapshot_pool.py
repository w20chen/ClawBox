from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from clawbox.experiments.snapshot_pool import (
    SnapshotKey, WarmCapacityError, WarmSnapshotPool, WarmSnapshotTooLarge,
)


def key(index: int) -> SnapshotKey:
    return SnapshotKey(f"session-{index}", "tool", 1)


def commit(pool: WarmSnapshotPool, index: int, size: int) -> None:
    pool.reserve(key(index), size)
    pool.commit(key(index), path=f"/warm/{index}", logical_bytes=size,
                allocated_bytes=size, transferred_bytes=size)


def test_reservation_and_commit_never_double_count() -> None:
    pool = WarmSnapshotPool(100)
    pool.reserve(key(1), 60)
    assert (pool.reserved_bytes, pool.committed_bytes) == (60, 0)
    pool.commit(key(1), path="/warm/1", logical_bytes=55,
                allocated_bytes=50, transferred_bytes=55)
    assert (pool.reserved_bytes, pool.committed_bytes) == (0, 50)


def test_oversize_and_capacity_pressure_fail_closed() -> None:
    pool = WarmSnapshotPool(100)
    with pytest.raises(WarmSnapshotTooLarge):
        pool.reserve(key(1), 101)
    commit(pool, 1, 70)
    with pytest.raises(WarmCapacityError):
        pool.reserve(key(2), 40)


def test_lru_victims_are_pinned_and_deterministic() -> None:
    pool = WarmSnapshotPool(100, clock=lambda: 1.0)
    commit(pool, 2, 30)
    commit(pool, 1, 30)
    victims = pool.lru_victims(60)
    assert [item.key for item in victims] == [key(1)]
    with pytest.raises(RuntimeError, match="pinned"):
        pool.remove(key(1))
    for item in victims:
        pool.unpin(item.key)
        pool.remove(item.key)
    pool.reserve(key(3), 60)


def test_concurrent_reservations_conserve_capacity() -> None:
    pool = WarmSnapshotPool(64)

    def reserve(index: int) -> bool:
        try:
            pool.reserve(key(index), 16)
            return True
        except WarmCapacityError:
            return False

    with ThreadPoolExecutor(max_workers=16) as executor:
        accepted = list(executor.map(reserve, range(16)))
    assert sum(accepted) == 4
    assert pool.reserved_bytes == 64


def test_spill_for_admission_relocates_lru_and_reclaims_accounting() -> None:
    pool = WarmSnapshotPool(100, clock=lambda: 1.0)
    spilled: list[SnapshotKey] = []
    for index in (1, 2):
        item_key = key(index)
        pool.reserve(item_key, 50)
        pool.commit(
            item_key, path=f"/warm/{index}", logical_bytes=50,
            allocated_bytes=50, transferred_bytes=50,
            spiller=lambda item: spilled.append(item.key),
        )
    victims = pool.spill_for_admission(50)
    assert [item.key for item in victims] == [key(1)]
    assert spilled == [key(1)]
    assert pool.committed_bytes == 50


def test_oversized_admission_never_spills_or_pins_existing_snapshots() -> None:
    pool = WarmSnapshotPool(100)
    spilled: list[SnapshotKey] = []
    pool.reserve(key(1), 100)
    pool.commit(
        key(1), path="/warm/1", logical_bytes=100, allocated_bytes=100,
        transferred_bytes=100, spiller=lambda item: spilled.append(item.key),
    )
    before = pool.snapshot()
    with pytest.raises(WarmSnapshotTooLarge):
        pool.spill_for_admission(101)
    assert spilled == []
    assert pool.snapshot() == before


def test_consumer_pin_excludes_spill_and_can_retire_snapshot() -> None:
    pool = WarmSnapshotPool(100)
    commit(pool, 1, 100)
    with pool.consume_guard(key(1)) as item:
        assert item is not None and item.pinned == 1
        with pytest.raises(WarmCapacityError):
            pool.spill_for_admission(1)
        pool.remove(key(1), consume_pinned=True)
    assert pool.committed_bytes == 0


def test_consumer_waits_for_inflight_spill() -> None:
    pool = WarmSnapshotPool(100)
    entered, release, consumed = Event(), Event(), Event()

    def spill(item):
        entered.set()
        assert release.wait(5)

    pool.reserve(key(1), 100)
    pool.commit(key(1), path="/warm/1", logical_bytes=100,
                allocated_bytes=100, transferred_bytes=100, spiller=spill)

    def consume():
        with pool.consume_guard(key(1)) as item:
            consumed.set()
            return item

    with ThreadPoolExecutor(max_workers=2) as executor:
        spilling = executor.submit(pool.spill_for_admission, 100)
        assert entered.wait(5)
        consuming = executor.submit(consume)
        try:
            assert not consumed.wait(0.1)
        finally:
            release.set()
        assert len(spilling.result(timeout=5)) == 1
        assert consuming.result(timeout=5) is None
    assert pool.committed_bytes == 0


def test_consumer_failure_releases_pin_without_retiring_snapshot() -> None:
    pool = WarmSnapshotPool(100)
    commit(pool, 1, 100)
    with pytest.raises(ValueError):
        with pool.consume_guard(key(1)):
            raise ValueError("restore failed")
    assert pool.committed_bytes == 100
    assert pool.snapshot()["manifests"][0]["pinned"] == 0
