from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from clawbox.experiments.policy import AdmissionTimeout, PolicyCoordinator
from clawbox.experiments.spec import PolicySpec
from clawbox.experiments.spec_types import SnapshotTier


class Lifecycle:
    def __init__(self) -> None:
        self._resident = True
        self.pauses = 0

    @property
    def resident(self) -> bool:
        return self._resident

    def checkpoint_and_evict(self) -> float:
        self._resident = False
        self.pauses += 1
        return 0.01


def test_admission_reclaims_real_cache_before_pausing_a_vm() -> None:
    used = [9 * 1024**2]
    calls = []
    def reclaim() -> None:
        calls.append(True)
        used[0] = 5 * 1024**2
    policy = PolicySpec(name="resident", admission="tool_full", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                                    operation_headroom_mib=1, reclaim_cache=reclaim,
                                    physical_sample=lambda: (used[0], 100 * 1024**2))
    coordinator.acquire("restore", 4, 1, wait_class="restore")
    assert calls == [True]
    coordinator.release("restore", 4)


def test_unsuccessful_cache_reclaim_does_not_invent_capacity() -> None:
    policy = PolicySpec(name="resident", admission="tool_full", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                                    operation_headroom_mib=1, reclaim_cache=lambda: None,
                                    physical_sample=lambda: (9 * 1024**2, 100 * 1024**2))
    with pytest.raises(AdmissionTimeout):
        coordinator.acquire("restore", 4, 0, wait_class="restore")


def test_slow_kernel_reclaim_does_not_hold_the_admission_head() -> None:
    used = [9 * 1024**2]
    finish = threading.Event()
    def reclaim() -> None:
        used[0] = 5 * 1024**2
        assert finish.wait(3)
    policy = PolicySpec(name="resident", admission="tool_full", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                                    operation_headroom_mib=1, reclaim_cache=reclaim,
                                    physical_sample=lambda: (used[0], 100 * 1024**2))
    try:
        coordinator.acquire("restore", 4, 1, wait_class="restore")
        assert coordinator._cache_reclaim_running
        coordinator.release("restore", 4)
    finally:
        finish.set()


def test_running_tool_can_pass_a_capacity_blocked_new_pair() -> None:
    policy = PolicySpec(name="resident", admission="tool_full", reclamation="resident",
                        eviction="none", restore="none")
    used = [8 * 1024**2]
    coordinator = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                                    operation_headroom_mib=0,
                                    physical_sample=lambda: (used[0], 100 * 1024**2))
    created = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(coordinator.materialize, "new-pair", 6,
                              lambda: created.set() or 0.01, 2)
        deadline = time.monotonic() + 1
        while not coordinator._waiters and time.monotonic() < deadline:
            time.sleep(0.001)
        assert coordinator._waiters
        # The old single FIFO times out here despite enough room for this tool.
        coordinator.acquire("running", 1, 0.5)
        assert not created.is_set()
        coordinator.release("running", 1)
        used[0] = 0
        pending.result(timeout=2)
    assert created.is_set()


def test_new_pair_leaves_room_for_a_tool_to_make_progress() -> None:
    policy = PolicySpec(name="resident", admission="tool_full", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                                    operation_headroom_mib=1, startup_headroom_mib=2,
                                    physical_sample=lambda: (2 * 1024**2, 100 * 1024**2))
    with pytest.raises(AdmissionTimeout):
        coordinator.materialize("new-pair", 6, lambda: pytest.fail("must not start"), 0)
    coordinator.acquire("running", 6, 0)
    coordinator.release("running", 6)


def test_resident_policy_never_selects_or_pauses_a_victim() -> None:
    policy = PolicySpec(name="resident", admission="lifetime_full", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(policy, budget_mib=1, emergency_free_mib=1,
                                    operation_headroom_mib=0,
                                    physical_sample=lambda: (2 * 1024**2, 100 * 1024**2))
    victim = Lifecycle()
    coordinator.register("victim", victim)
    coordinator.set_eviction_eligible("victim", True)
    assert coordinator.victim_for_restore("requester") is None
    with pytest.raises(AdmissionTimeout):
        coordinator.acquire("requester", 1, 0)
    assert victim.pauses == 0
    metrics = coordinator.admission_metrics()
    assert metrics["safety_intervention_count"] == 1
    assert metrics["safety_interventions_by_reason"] == {
        "configured_memory_budget": 1,
    }


def test_snapshot_policy_uses_only_idle_eligible_lru_victim() -> None:
    policy = PolicySpec(name="snapshot", admission="tool_static", reclamation="snapshot_pause",
                        eviction="wait_aware_pressure", restore="reactive")
    coordinator = PolicyCoordinator(policy, budget_mib=1, emergency_free_mib=1,
                                    operation_headroom_mib=0,
                                    physical_sample=lambda: (2 * 1024**2, 100 * 1024**2))
    active, idle = Lifecycle(), Lifecycle()
    coordinator.register("active", active)
    coordinator.register("idle", idle)
    coordinator.set_tool_active("active", True)
    coordinator.set_eviction_eligible("idle", True)
    victim = coordinator.victim_for_restore("requester")
    assert victim is not None and victim.session_id == "idle"
    coordinator.release_victim(victim)


def test_tool_admission_protects_resident_tool_before_memory_wait() -> None:
    """A delayed admission cannot be selected for pause by another waiter."""
    policy = PolicySpec(name="snapshot", admission="tool_static",
                        reclamation="snapshot_pause", eviction="eager",
                        restore="reactive")
    entered = threading.Event()

    def pressured_sample() -> tuple[int, int]:
        entered.set()
        return 2 * 1024**2, 100 * 1024**2

    coordinator = PolicyCoordinator(
        policy, budget_mib=1, emergency_free_mib=1, operation_headroom_mib=0,
        physical_sample=pressured_sample,
    )
    lifecycle = Lifecycle()
    coordinator.register("tool", lifecycle)
    coordinator.set_eviction_eligible("tool", True)
    result: list[BaseException] = []

    def delayed_admission() -> None:
        try:
            coordinator.begin_tool_admission("tool", 1, 0.05)
        except BaseException as exc:
            result.append(exc)

    thread = threading.Thread(target=delayed_admission)
    thread.start()
    assert entered.wait(timeout=1)
    assert coordinator.tool_active("tool")
    assert coordinator.victim_for_restore("other") is None
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert result and isinstance(result[0], AdmissionTimeout)
    victim = coordinator.victim_for_restore("other")
    assert victim is not None and victim.session_id == "tool"
    coordinator.release_victim(victim)


def test_tool_activation_waits_for_an_atomic_eviction_claim() -> None:
    policy = PolicySpec(name="snapshot", admission="tool_static",
                        reclamation="snapshot_pause", eviction="eager",
                        restore="reactive")
    coordinator = PolicyCoordinator(policy, budget_mib=1, emergency_free_mib=1,
                                    operation_headroom_mib=0)
    lifecycle = Lifecycle()
    coordinator.register("tool", lifecycle)
    coordinator.set_eviction_eligible("tool", True)
    victim = coordinator.victim_for_restore("other")
    assert victim is not None
    assert coordinator.victim_for_restore("third") is None

    activated = threading.Event()
    thread = threading.Thread(
        target=lambda: (coordinator.set_tool_active("tool", True), activated.set())
    )
    thread.start()
    assert not activated.wait(timeout=0.05)
    coordinator.release_victim(victim)
    assert activated.wait(timeout=1)
    thread.join(timeout=1)
    assert coordinator.tool_active("tool")


def test_admission_is_fifo_and_exports_overhead_metrics() -> None:
    policy = PolicySpec(name="resident", admission="tool_static", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(
        policy, budget_mib=1, emergency_free_mib=1, operation_headroom_mib=0,
        physical_sample=lambda: (0, 100 * 1024**2),
    )
    coordinator.acquire("holder", 1, 1)
    order: list[str] = []

    def queued(session_id: str) -> None:
        coordinator.acquire(session_id, 1, 2)
        order.append(session_id)
        coordinator.release(session_id, 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        second = pool.submit(queued, "second")
        time.sleep(0.03)
        third = pool.submit(queued, "third")
        time.sleep(0.03)
        coordinator.release("holder", 1)
        second.result()
        third.result()

    assert order == ["second", "third"]
    metrics = coordinator.admission_metrics()
    assert metrics["discipline"] == "progress_before_create_fifo"
    assert metrics["admission_count"] == 3
    assert metrics["max_queue_depth"] == 2
    assert metrics["wait_p95_seconds"] is not None


def test_lifecycle_reservations_do_not_pollute_tool_admission_metrics() -> None:
    policy = PolicySpec(name="resident", admission="tool_static", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(
        policy, budget_mib=5, emergency_free_mib=1, operation_headroom_mib=1,
        physical_sample=lambda: (0, 100 * 1024**2),
    )

    def create_while_reserved() -> float:
        with pytest.raises(AdmissionTimeout):
            coordinator.acquire("other", 1, 0)
        return 0.25

    def restore_while_reserved() -> float:
        with pytest.raises(AdmissionTimeout):
            coordinator.acquire("other", 1, 0)
        return 0.5

    created, create_wait = coordinator.materialize("session", 4, create_while_reserved, 1)
    restored, restore_wait = coordinator.restore("session", 4, restore_while_reserved, 1)
    assert (created, restored) == (0.25, 0.5)
    assert create_wait >= 0 and restore_wait >= 0
    # Both lifecycle reservations have been released after their operations.
    coordinator.acquire("other", 1, 1)
    coordinator.release("other", 1)
    metrics = coordinator.admission_metrics()
    # Failed nested probes plus the final successful Tool acquire are counted;
    # create/restore waits themselves are kept in separate lifecycle metrics.
    assert metrics["admission_count"] == 3
    assert metrics["lifecycle_create_reservation_wait_seconds"] >= 0
    assert metrics["lifecycle_restore_reservation_wait_seconds"] >= 0


def test_incremental_reservation_is_added_to_observed_host_memory() -> None:
    policy = PolicySpec(name="resident", admission="tool_static", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(
        policy, budget_mib=10, emergency_free_mib=1, operation_headroom_mib=0,
        physical_sample=lambda: (8 * 1024**2, 100 * 1024**2),
    )
    with pytest.raises(AdmissionTimeout):
        coordinator.acquire("tool", 3, 0)


def test_lifetime_capacity_claim_is_not_double_counted_after_materialization() -> None:
    policy = PolicySpec(name="conservative", admission="lifetime_full",
                        reclamation="resident", eviction="none", restore="none")
    used = [0]
    coordinator = PolicyCoordinator(
        policy, budget_mib=10, emergency_free_mib=1, operation_headroom_mib=0,
        physical_sample=lambda: (used[0], 100 * 1024**2),
    )
    coordinator.acquire_capacity("agent", 8, 1)
    used[0] = 8 * 1024**2
    coordinator.acquire("agent", 2, 1)
    coordinator.release("agent", 2)
    coordinator.release_capacity("agent", 8)


def test_wait_plan_uses_only_request_time_prediction() -> None:
    policy = PolicySpec(
        name="predicted", admission="tool_static", reclamation="snapshot_pause",
        eviction="wait_aware_pressure", restore="proactive",
        prefetch_lead_seconds=0.5,
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=1, emergency_free_mib=1, operation_headroom_mib=0,
        physical_sample=lambda: (2 * 1024**2, 100 * 1024**2),
    )
    # A missing estimate cannot be replaced with the held-out actual duration.
    assert coordinator.model_wait_plan(None) == (None, None)
    assert coordinator.model_wait_plan(3.0) == (0.0, 0.5)

    fixed = PolicySpec(
        name="fixed", admission="tool_static", reclamation="snapshot_pause",
        eviction="fixed_delay", restore="reactive", fixed_delay_seconds=0.25,
    )
    fixed_coordinator = PolicyCoordinator(
        fixed, budget_mib=1, emergency_free_mib=1, operation_headroom_mib=0,
    )
    assert fixed_coordinator.model_wait_plan(None) == (0.25, None)


def test_time_oracle_wait_plan_uses_actual_replay_duration_at_break_even() -> None:
    policy = PolicySpec(
        name="time-oracle", admission="tool_static",
        reclamation="snapshot_pause", eviction="time_oracle",
        restore="reactive", checkpoint_break_even_seconds=4.0,
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=1, emergency_free_mib=1,
        operation_headroom_mib=0,
    )

    assert coordinator.model_wait_plan(None, oracle_duration_s=3.999) == (
        None, None,
    )
    assert coordinator.model_wait_plan(None, oracle_duration_s=4.0) == (
        0.0, None,
    )


def test_tiered_time_oracle_uses_exact_two_and_twenty_second_boundaries() -> None:
    policy = PolicySpec(
        name="tiered-time", admission="tool_p90", reclamation="snapshot_pause",
        eviction="tiered_time_oracle", restore="reactive",
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=64, emergency_free_mib=1, operation_headroom_mib=0,
    )
    assert coordinator.oracle_tier(1.999) is SnapshotTier.LOCAL
    assert coordinator.oracle_tier(2.0) is SnapshotTier.WARM
    assert coordinator.oracle_tier(19.999) is SnapshotTier.WARM
    assert coordinator.oracle_tier(20.0) is SnapshotTier.COLD
    assert coordinator.model_wait_plan(None, oracle_duration_s=1.999) == (None, None)
    assert coordinator.model_wait_plan(None, oracle_duration_s=2.0) == (0.0, None)


def test_tiered_lru_prefers_remaining_wait_of_at_least_two_seconds() -> None:
    policy = PolicySpec(
        name="tiered-lru", admission="tool_p90", reclamation="snapshot_pause",
        eviction="tiered_lru_oracle", restore="reactive",
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=64, emergency_free_mib=1, operation_headroom_mib=0,
    )
    short, useful = Lifecycle(), Lifecycle()
    coordinator.register("old-short", short)
    coordinator.begin_model_wait("old-short", "wait-short", 1.0)
    coordinator.register("new-useful", useful)
    coordinator.begin_model_wait("new-useful", "wait-useful", 3.0)
    victim = coordinator.victim_for_restore("requester")
    assert victim is not None and victim.session_id == "new-useful"
    assert coordinator.pressure_oracle_tier(victim) is SnapshotTier.WARM
    coordinator.release_victim(victim)
