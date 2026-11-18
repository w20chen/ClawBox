from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from clawbox.experiments.snapshot_pool import (
    SnapshotKey, WarmAdmissionRejected, WarmCapacityError, WarmSnapshotPool,
    WarmSnapshotTooLarge,
)


def key(index: int) -> SnapshotKey:
    return SnapshotKey(f"session-{index}", "tool", 1)


def test_cold_children_keep_one_physical_warm_charge_until_native_gc(tmp_path):
    from clawbox.cube.snapshot import _allocated_bytes
    layer = tmp_path / 'managed.mem'
    layer.write_bytes(b'x' * 4096)
    stat = layer.stat()
    size = _allocated_bytes(layer, stat)
    identity = f'{stat.st_dev}:{stat.st_ino}'
    dependency = {'identity': identity, 'path': str(layer), 'allocated_bytes': size}
    pool = WarmSnapshotPool(size * 3)
    pool.reserve(key(1), size)
    pool.commit(key(1), path=str(layer), logical_bytes=4096,
                allocated_bytes=size, transferred_bytes=size,
                owned_layer_identities=(identity,))
    pool.retain_for_vm(key(1))
    pool.commit_cold_dependencies([dependency], replaces=key(1))
    assert pool.committed_bytes == pool.accounted_bytes == size
    assert not pool.snapshot()['manifests']
    # Another COLD child referencing the same physical layer adds no charge.
    pool.commit_cold_dependencies([dependency])
    assert pool.committed_bytes == size
    with pytest.raises(WarmCapacityError):
        pool.reserve(key(2), size * 3)
    # Losing a logical owner alone cannot free physical capacity.
    pool.commit_cold_dependencies([])
    pool.refresh_managed_warm_layers()
    assert pool.committed_bytes == size
    layer.unlink()  # native GC after the final reference is removed
    pool.refresh_managed_warm_layers()
    assert pool.committed_bytes == pool.accounted_bytes == 0
    pool.reserve(key(2), size * 3)


def test_managed_warm_inode_is_not_counted_twice_with_an_owned_alias(tmp_path):
    from clawbox.cube.snapshot import _allocated_bytes
    layer = tmp_path / 'managed.mem'
    layer.write_bytes(b'x' * 4096)
    stat = layer.stat()
    size = _allocated_bytes(layer, stat)
    identity = f'{stat.st_dev}:{stat.st_ino}'
    dependency = {'identity': identity, 'path': str(layer), 'allocated_bytes': size}
    pool = WarmSnapshotPool(size * 3)
    pool.reserve(key(1), size)
    pool.commit(key(1), path=str(layer), logical_bytes=4096,
                allocated_bytes=size, transferred_bytes=size,
                owned_layer_identities=(identity,), managed_warm_layers=(dependency,))
    assert pool.accounted_bytes == size
    # Removing the owned alias does not free the managed ancestor.
    pool.remove(key(1))
    assert pool.accounted_bytes == size


@pytest.mark.skipif(__import__('os').name != 'posix', reason='Linux hole punching')
def test_shared_page_reclaim_updates_owned_and_managed_warm_capacity(tmp_path):
    import ctypes
    import os
    from clawbox.cube.snapshot import _allocated_bytes
    page = os.sysconf('SC_PAGE_SIZE')
    layer = tmp_path / 'shared.mem'
    layer.write_bytes(b'x' * (4 * page))
    size = _allocated_bytes(layer)
    stat = layer.stat()
    identity = f'{stat.st_dev}:{stat.st_ino}'
    pool = WarmSnapshotPool(16 * page)
    for i in (1, 2):
        pool.reserve(key(i), size)
        pool.commit(key(i), path=str(layer), logical_bytes=4*page,
                    allocated_bytes=size, transferred_bytes=size,
                    owned_layer_identities=(identity,),
                    owned_layer_files={identity: (str(layer), size)})
    assert pool.committed_bytes == size
    pool.commit_cold_dependencies([{'identity': identity, 'path': str(layer), 'allocated_bytes': size}])
    assert pool.committed_bytes == size
    libc = ctypes.CDLL(None, use_errno=True)
    with layer.open('r+b') as stream:
        assert libc.fallocate(stream.fileno(), 3, ctypes.c_longlong(page), ctypes.c_longlong(3*page)) == 0
    actual = _allocated_bytes(layer)
    assert actual == page
    pool.refresh_managed_warm_layers()
    assert pool.committed_bytes == pool.accounted_bytes == actual
    pool.validate_expanded(key(1), actual)
    # Refreshing physical usage must not bypass the existing growth budget.
    with layer.open('r+b') as stream:
        stream.seek(page)
        stream.write(b'y' * page)
    with pytest.raises(RuntimeError, match='exceeds runtime reserve'):
        pool.refresh_managed_warm_layers()
    with layer.open('r+b') as stream:
        assert libc.fallocate(stream.fileno(), 3, ctypes.c_longlong(page), ctypes.c_longlong(page)) == 0
    pool.remove(key(1))
    pool.remove(key(2))
    assert pool.committed_bytes == actual
    layer.unlink()
    pool.refresh_managed_warm_layers()
    assert pool.accounted_bytes == 0


def test_borrow_headroom_counts_all_shared_occupancy_and_rechecks_at_commit():
    pool = WarmSnapshotPool(100, borrow_capacity_bytes=60,
                            external_shared_live_bytes=10, external_warm_bytes=5)
    assert pool.borrow_headroom_bytes == 60
    pool.reserve_borrow('existing', 20)
    pool.reserve(key(1), 50)
    assert pool.borrow_headroom_bytes == 15
    pool.commit(key(1), path='/warm/1', logical_bytes=50,
                allocated_bytes=40, transferred_bytes=40)
    assert pool.borrow_headroom_bytes == 25
    pool.reserve(key(2), 20)
    assert pool.borrow_headroom_bytes == 5
    with pytest.raises(WarmCapacityError):
        pool.reserve_borrow('stale-choice', 25)


def commit(pool: WarmSnapshotPool, index: int, size: int) -> None:
    pool.reserve(key(index), size)
    pool.commit(key(index), path=f"/warm/{index}", logical_bytes=size,
                allocated_bytes=size, transferred_bytes=size)


def commit_next_use(
    pool: WarmSnapshotPool, index: int, size: int, deadline: float | None,
    spilled: list | None = None,
) -> None:
    pool.reserve(key(index), size)
    pool.commit(
        key(index), path=f"/warm/{index}", logical_bytes=size,
        allocated_bytes=size, transferred_bytes=size,
        next_use_monotonic_s=deadline,
        spiller=(spilled.append if spilled is not None else None),
    )


def test_next_use_admission_evicts_strictly_later_snapshots_latest_first():
    spilled = []
    pool = WarmSnapshotPool(100, next_use_aware=True)
    commit_next_use(pool, 1, 40, 20, spilled)
    commit_next_use(pool, 2, 40, 50, spilled)
    pool.reserve_for_admission(
        key(3), 50, timeout_s=.01, next_use_monotonic_s=10,
    )
    assert [item.key for item in spilled] == [key(2)]
    assert pool.reserved_bytes == 50
    assert pool.snapshot()["residency_policy"] == "next_use"


@pytest.mark.parametrize("victim,candidate", [(10, 10), (9, 10), (None, 10)])
def test_next_use_admission_rejects_equal_earlier_or_unknown_victim(victim, candidate):
    pool = WarmSnapshotPool(100, next_use_aware=True)
    commit_next_use(pool, 1, 100, victim, [])
    with pytest.raises(WarmAdmissionRejected):
        pool.reserve_for_admission(
            key(2), 1, timeout_s=.01, next_use_monotonic_s=candidate,
        )
    assert pool.manifest(key(1)).pinned == 0


def test_next_use_admission_requires_enough_actual_reclaimable_bytes():
    spilled = []
    pool = WarmSnapshotPool(120, next_use_aware=True)
    # The shared identity remains owned by the second manifest, so spilling
    # the latest first snapshot reclaims no physical bytes.
    shared = "1:1"
    for index, deadline in ((1, 40), (2, 30)):
        pool.reserve(key(index), 50)
        pool.commit(
            key(index), path=f"/warm/{index}", logical_bytes=50,
            allocated_bytes=50, transferred_bytes=50,
            next_use_monotonic_s=deadline, spiller=spilled.append,
            owned_layer_identities=(shared,),
            owned_layer_files={shared: ("/warm/shared", 50)},
        )
    pool.reserve_for_admission(
        key(3), 80, timeout_s=.01, next_use_monotonic_s=10,
    )
    assert [item.key for item in spilled] == [key(1), key(2)]


def test_next_use_fit_needs_no_estimate_and_revision_updates_are_ordered():
    pool = WarmSnapshotPool(100, next_use_aware=True)
    pool.reserve_for_admission(key(1), 50, timeout_s=.01)
    pool.commit(
        key(1), path="/warm/1", logical_bytes=50, allocated_bytes=50,
        transferred_bytes=50, next_use_monotonic_s=20, next_use_revision=2,
    )
    pool.update_next_use(key(1), 99, revision=1)
    assert pool.manifest(key(1)).next_use_monotonic_s == 20
    pool.update_next_use(key(1), 30, revision=3)
    assert pool.manifest(key(1)).next_use_monotonic_s == 30


def test_next_use_physical_safety_spill_bypasses_deadline_filter():
    spilled = []
    pool = WarmSnapshotPool(
        100, borrow_capacity_bytes=100, borrow_resident_headroom_bytes=10,
        next_use_aware=True,
    )
    commit_next_use(pool, 1, 70, None, spilled)
    pool.reserve_borrow("vm", 20)
    pool.observe_shared_live(30, settle_borrow="vm")
    assert pool.spill_observed_excess()[0].key == key(1)
    assert [item.key for item in spilled] == [key(1)]


def test_slow_spill_does_not_hold_admission_lock():
    checked = Event()
    pool = WarmSnapshotPool(100, next_use_aware=True)

    def spill(_item):
        assert pool._admission_lock.acquire(blocking=False)
        pool._admission_lock.release()
        checked.set()

    pool.reserve(key(1), 100)
    pool.commit(
        key(1), path="/warm/1", logical_bytes=100, allocated_bytes=100,
        transferred_bytes=100, next_use_monotonic_s=20, spiller=spill,
    )
    pool.reserve_for_admission(
        key(2), 100, timeout_s=.01, next_use_monotonic_s=10,
    )
    assert checked.is_set()


def test_snapshot_aware_rejects_lower_value_candidate_without_spilling():
    pool = WarmSnapshotPool(100, snapshot_aware=True)
    pool.reserve(key(1), 100)
    pool.commit(key(1), path="/warm/1", logical_bytes=100,
                allocated_bytes=100, transferred_bytes=100,
                retention_value_seconds=10)
    with pytest.raises(WarmAdmissionRejected):
        pool.reserve_for_admission(
            key(2), 100, timeout_s=.01, retention_value_seconds=5,
        )
    assert pool.manifest(key(1)).path == "/warm/1"


def test_snapshot_aware_spills_lower_value_state_and_reports_base_refs():
    pool = WarmSnapshotPool(100, snapshot_aware=True)
    pool.reserve(key(1), 100)
    spilled=[]
    pool.commit(key(1), path="/warm/1", logical_bytes=150,
                allocated_bytes=100, transferred_bytes=100,
                retention_value_seconds=2, template_id="tpl-a",
                shared_base_identity="base-a", private_delta_bytes=100,
                dependency_count=1, spiller=spilled.append)
    pool.reserve_for_admission(
        key(2), 100, timeout_s=.01, retention_value_seconds=8,
    )
    assert [item.key for item in spilled] == [key(1)]
    assert pool.snapshot()["shared_base_references"] == {}


def test_reservation_and_commit_never_double_count() -> None:
    pool = WarmSnapshotPool(100)
    pool.reserve(key(1), 60)
    assert (pool.reserved_bytes, pool.committed_bytes) == (60, 0)
    pool.commit(key(1), path="/warm/1", logical_bytes=55,
                allocated_bytes=50, transferred_bytes=55)
    assert (pool.reserved_bytes, pool.committed_bytes) == (0, 50)


def test_live_borrow_and_snapshots_share_one_capacity() -> None:
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)
    pool.reserve_borrow("vm-a", 60)
    assert pool.borrowed_bytes == 60
    pool.reserve(key(1), 68)
    with pytest.raises(WarmCapacityError, match="shared NUMA capacity"):
        pool.reserve_borrow("vm-b", 1)
    pool.abort(key(1))
    assert pool.release_borrow("vm-a") == 60
    assert pool.snapshot()["borrowed_bytes"] == 0


def test_live_borrow_cannot_exceed_half_pool_limit() -> None:
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)
    pool.reserve_borrow("vm-a", 64)
    with pytest.raises(WarmCapacityError, match="live-borrow"):
        pool.reserve_borrow("vm-b", 1)


def test_preexisting_shared_cache_and_files_remain_inside_the_same_capacity():
    pool = WarmSnapshotPool(100, borrow_capacity_bytes=100,
                            external_shared_live_bytes=4, external_warm_bytes=2)
    pool.reserve_borrow("vm-a", 3)
    pool.reserve(key(1), 91)
    with pytest.raises(WarmCapacityError):
        pool.reserve(key(2), 1)
    with pytest.raises(WarmCapacityError):
        pool.reserve_borrow("vm-b", 1)
    pool.commit(key(1), path="/warm/1", logical_bytes=91,
                allocated_bytes=91, transferred_bytes=91)
    assert pool.snapshot()["capacity_bytes"] == 100
    assert pool.snapshot()["external_occupied_bytes"] == 6
    assert pool.committed_bytes + pool.borrowed_bytes + 6 == 100


def test_resident_borrow_accounting_keeps_full_charge_until_observed():
    pool = WarmSnapshotPool(
        100, borrow_capacity_bytes=100, borrow_resident_headroom_bytes=10,
        external_shared_live_bytes=5, external_warm_bytes=2,
    )
    pool.reserve_borrow("vm-a", 60)
    assert pool.borrowed_bytes == 60
    assert pool.snapshot()["unobserved_borrows"] == ["vm-a"]
    assert pool.accounted_bytes == 77

    assert pool.observe_shared_live(8, settle_borrow="vm-a") == 0
    state = pool.snapshot()
    assert state["borrowed_bytes"] == 60  # configured-limit ledger is unchanged
    assert state["borrow_accounted_bytes"] == 18
    assert state["accounted_bytes"] == 20
    assert state["unobserved_borrows"] == []

    fresh = WarmSnapshotPool(
        100, borrow_capacity_bytes=100, borrow_resident_headroom_bytes=10,
    )
    with pytest.raises(WarmCapacityError, match="shared NUMA"):
        fresh.reserve_borrow("too-large-during-rebind", 91)


def test_resident_growth_spills_warm_state_before_physical_capacity():
    pool = WarmSnapshotPool(
        100, borrow_capacity_bytes=100, borrow_resident_headroom_bytes=10,
        snapshot_aware=True,
    )
    pool.reserve_borrow("vm-a", 60)
    pool.observe_shared_live(20, settle_borrow="vm-a")
    spilled = []
    pool.reserve(key(1), 60)
    pool.commit(
        key(1), path="/warm/1", logical_bytes=60, allocated_bytes=60,
        transferred_bytes=60, retention_value_seconds=100,
        spiller=spilled.append,
    )
    assert pool.accounted_bytes == 90

    assert pool.observe_shared_live(35) == 5
    victims = pool.spill_observed_excess()
    assert [item.key for item in victims] == [key(1)]
    assert [item.key for item in spilled] == [key(1)]
    assert pool.accounted_bytes == 45


def test_external_occupancy_cannot_be_reclaimed_by_snapshot_spill():
    pool = WarmSnapshotPool(100, external_shared_live_bytes=4, external_warm_bytes=2)
    with pytest.raises(WarmSnapshotTooLarge):
        pool.reserve_for_admission(key(1), 95, timeout_s=.01)
    commit(pool, 1, 50)
    victims = pool.lru_victims(45)
    assert [item.key for item in victims] == [key(1)]


def test_retained_lineage_is_not_spillable_and_commit_counts_only_new_pages():
    pool = WarmSnapshotPool(100)
    commit(pool, 1, 60)
    pool.retain_for_vm(key(1))
    with pytest.raises(WarmCapacityError):
        pool.lru_victims(50)
    successor = SnapshotKey(key(1).session_id, "tool", 2)
    pool.reserve(successor, 30)
    pool.commit(successor, path="/warm/delta", logical_bytes=60,
                allocated_bytes=80, transferred_bytes=20, replaces=key(1))
    assert pool.reserved_bytes == 0
    assert pool.committed_bytes == 80
    assert len(pool.snapshot()["manifests"]) == 1
    assert not pool.manifest(successor).retained_by_vm


def test_failed_successor_commit_preserves_retained_baseline():
    pool = WarmSnapshotPool(100)
    commit(pool, 1, 60)
    pool.retain_for_vm(key(1))
    successor = SnapshotKey(key(1).session_id, "tool", 2)
    pool.reserve(successor, 30)
    with pytest.raises(RuntimeError, match="exceed reservation"):
        pool.commit(successor, path="/warm/delta", logical_bytes=60,
                    allocated_bytes=95, transferred_bytes=35, replaces=key(1))
    pool.abort(successor)
    assert pool.committed_bytes == 60
    assert pool.manifest(key(1)).retained_by_vm


def test_expansion_reservation_transitions_to_retained_raw_allocation() -> None:
    pool = WarmSnapshotPool(100)
    pool.reserve(key(1), 40)
    pool.commit(
        key(1), path="/warm/1", logical_bytes=80, allocated_bytes=10,
        transferred_bytes=80, compressed_allocated_bytes=10,
        raw_allocated_bytes=65, expansion_allocated_bytes=60, expanded=False,
    )
    assert pool.committed_bytes == 10

    with pool.consume_guard(key(1)):
        pool.reserve_expansion_for_restore(key(1), 60, timeout_s=.01)
        assert pool.committed_bytes == 10
        assert pool.expansion_reserved_bytes == 60
        assert pool.accounted_bytes == 70

        pool.finish_expansion(key(1), 65, retained_by_vm=True)
        manifest = pool.manifest(key(1))
        assert manifest.allocated_bytes == 65
        assert manifest.raw_allocated_bytes == 65
        assert manifest.compressed_allocated_bytes == 0
        assert manifest.expansion_allocated_bytes == 0
        assert manifest.expanded and manifest.retained_by_vm
        assert pool.expansion_reserved_bytes == 0
        assert pool.accounted_bytes == 65
        pool.finish_expansion(key(1), 65, retained_by_vm=True)
        assert pool.accounted_bytes == 65
        pool.validate_expanded(key(1), 65)
        with pytest.raises(RuntimeError, match="allocation changed"):
            pool.validate_expanded(key(1), 64)

    pool.remove(key(1))
    assert pool.accounted_bytes == 0


def test_runtime_page_materialization_consumes_preexisting_reserve() -> None:
    pool = WarmSnapshotPool(200)
    pool.reserve(key(1), 50)
    pool.commit(
        key(1), path="/warm/1", logical_bytes=150, allocated_bytes=10,
        transferred_bytes=150, compressed_allocated_bytes=10,
        raw_allocated_bytes=120, expansion_allocated_bytes=40,
        owned_logical_bytes=100, owned_allocated_bytes=40, expanded=False,
    )
    with pool.consume_guard(key(1)):
        pool.reserve_expansion_for_restore(key(1), 100, timeout_s=.01)
        pool.finish_expansion(
            key(1), 45, runtime_upper_bound_bytes=100,
            owned_allocated_bytes=40, retained_by_vm=True,
        )
        item = pool.manifest(key(1))
        assert item.runtime_reserved_bytes == 60
        assert pool.accounted_bytes == 105
        pool.validate_expanded(key(1), 50)
        item = pool.manifest(key(1))
        assert item.allocated_bytes == 50
        assert item.runtime_reserved_bytes == 55
        assert pool.accounted_bytes == 105


def test_expansion_reservation_is_idempotent_and_remove_releases_failed_stage() -> None:
    pool = WarmSnapshotPool(100)
    pool.reserve(key(1), 20)
    pool.commit(
        key(1), path="/warm/1", logical_bytes=80, allocated_bytes=10,
        transferred_bytes=80, compressed_allocated_bytes=10,
        raw_allocated_bytes=90, expansion_allocated_bytes=90, expanded=False,
    )
    with pytest.raises(RuntimeError, match="consumer pin"):
        pool.reserve_expansion_for_restore(key(1), 90, timeout_s=.01)
    with pool.consume_guard(key(1)):
        pool.reserve_expansion_for_restore(key(1), 90, timeout_s=.01)
        pool.reserve_expansion_for_restore(key(1), 90, timeout_s=.01)
        assert pool.expansion_reserved_bytes == 90
        with pytest.raises(ValueError, match="failed expansion"):
            raise ValueError("failed expansion")

    assert pool.manifest(key(1)).pinned == 0
    assert pool.expansion_reserved_bytes == 90
    with pytest.raises(WarmCapacityError, match="no spillable"):
        pool.lru_victims(1)
    pool.remove(key(1))
    assert pool.accounted_bytes == 0


def test_concurrent_expansions_cannot_overcommit_capacity() -> None:
    pool = WarmSnapshotPool(100)
    for index in (1, 2):
        pool.reserve(key(index), 10)
        pool.commit(
            key(index), path=f"/warm/{index}", logical_bytes=80,
            allocated_bytes=5, transferred_bytes=80,
            compressed_allocated_bytes=5, raw_allocated_bytes=85,
            expansion_allocated_bytes=80, expanded=False,
        )

    with pool.consume_guard(key(1)), pool.consume_guard(key(2)):
        pool.reserve_expansion_for_restore(key(1), 80, timeout_s=.01)
        with pytest.raises(WarmCapacityError, match="no spillable"):
            pool.reserve_expansion_for_restore(key(2), 80, timeout_s=.01)
        assert pool.expansion_reserved_bytes == 80
        assert pool.accounted_bytes == 90


def test_overlapping_restore_expansions_are_serialized_by_capacity() -> None:
    pool = WarmSnapshotPool(100)
    for index in (1, 2):
        pool.reserve(key(index), 10)
        pool.commit(
            key(index), path=f"/warm/{index}", logical_bytes=80,
            allocated_bytes=5, transferred_bytes=80,
            compressed_allocated_bytes=5, raw_allocated_bytes=85,
            expansion_allocated_bytes=80, expanded=False,
        )

    entered = Barrier(3)

    def reserve_expansion(index: int) -> str:
        entered.wait(timeout=5)
        try:
            pool.reserve_expansion_for_restore(key(index), 80, timeout_s=.05)
            return "admitted"
        except WarmCapacityError:
            return "rejected"

    with pool.consume_guard(key(1)), pool.consume_guard(key(2)):
        with ThreadPoolExecutor(max_workers=2) as executor:
            one = executor.submit(reserve_expansion, 1)
            two = executor.submit(reserve_expansion, 2)
            entered.wait(timeout=5)
            outcomes = [one.result(timeout=5), two.result(timeout=5)]

    assert sorted(outcomes) == ["admitted", "rejected"]
    assert pool.expansion_reserved_bytes == 80
    assert pool.accounted_bytes == 90


def test_snapshot_aware_restore_expansion_spills_without_candidate_rejection() -> None:
    pool = WarmSnapshotPool(100, snapshot_aware=True)
    spilled = []
    for index in (1, 2):
        pool.reserve(key(index), 40)
        pool.commit(
            key(index), path=f"/warm/{index}", logical_bytes=40,
            allocated_bytes=40, transferred_bytes=40,
            retention_value_seconds=1,
            spiller=lambda item: spilled.append(item.key),
        )

    with pool.consume_guard(key(1)):
        count = pool.reserve_expansion_for_restore(
            key(1), 30, timeout_s=.01,
        )

    assert count == 1
    assert spilled == [key(2)]
    assert pool.expansion_reserved_bytes == 30
    assert pool.accounted_bytes == 70


def test_compressed_spill_without_expansion_headroom_fails_closed() -> None:
    pool = WarmSnapshotPool(100, clock=lambda: 1.0)
    pool.reserve(key(1), 10)
    pool.commit(
        key(1), path="/warm/1", logical_bytes=80, allocated_bytes=10,
        transferred_bytes=80, compressed_allocated_bytes=10,
        raw_allocated_bytes=90, expansion_allocated_bytes=90, expanded=False,
        spiller=lambda item: pool.reserve_selected_expansion(
            item.key, item.expansion_allocated_bytes,
        ),
    )
    commit(pool, 2, 90)

    with pytest.raises(WarmCapacityError, match="compressed spill cannot expand"):
        pool.spill_for_admission(1)

    assert pool.committed_bytes == 100
    assert pool.expansion_reserved_bytes == 0
    assert pool.manifest(key(1)).pinned == 0


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


def test_snapshot_limit_leaves_total_lifecycle_capacity_for_create() -> None:
    pool = WarmSnapshotPool(
        100, transfer_concurrency=2, snapshot_transfer_concurrency=1,
        transfer_timeout_seconds=.01,
    )
    with pool.transfer("checkpoint"):
        # The independent total slot remains available for Cube create.
        with pool.transfer("create"):
            pass
        with pytest.raises(TimeoutError, match="snapshot slot timed out"):
            with pool.transfer("restore"):
                pass
    with pool.transfer("restore"):
        pass
    assert pool.snapshot()["snapshot_transfer_concurrency"] == 1


def test_snapshot_limit_cannot_exceed_total_lifecycle_limit() -> None:
    with pytest.raises(ValueError, match="between one and total"):
        WarmSnapshotPool(
            100, transfer_concurrency=2, snapshot_transfer_concurrency=3,
        )


def test_atomic_admission_waits_for_uncommitted_writer() -> None:
    pool = WarmSnapshotPool(100)
    pool.reserve(key(1), 100)
    admitted = Event()

    def admit():
        pool.reserve_for_admission(key(2), 100, timeout_s=5)
        admitted.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(admit)
        assert not admitted.wait(0.1)
        pool.commit(key(1), path="/warm/1", logical_bytes=100,
                    allocated_bytes=100, transferred_bytes=100, spiller=lambda _: None)
        future.result(timeout=5)
    assert pool.reserved_bytes == 100 and pool.committed_bytes == 0
