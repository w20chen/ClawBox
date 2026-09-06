from concurrent.futures import ThreadPoolExecutor

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
    pool = WarmSnapshotPool(100)
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
