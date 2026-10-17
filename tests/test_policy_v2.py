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
    policy = PolicySpec(name="resident", admission="fixed", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                                    operation_headroom_mib=1, reclaim_cache=reclaim,
                                    physical_sample=lambda: (used[0], 100 * 1024**2))
    coordinator.acquire("restore", 4, 1, wait_class="restore")
    assert calls == [True]
    coordinator.release("restore", 4)


def test_unbounded_admission_waits_until_capacity_is_available() -> None:
    used = [9 * 1024**2]
    policy = PolicySpec(name="resident", admission="fixed", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                                    operation_headroom_mib=1,
                                    physical_sample=lambda: (used[0], 100 * 1024**2))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(coordinator.acquire, "restore", 4, None)
        time.sleep(0.05)
        assert not future.done()
        used[0] = 0
        assert future.result(timeout=2) >= 0.05
    coordinator.release("restore", 4)


def test_unsuccessful_cache_reclaim_does_not_invent_capacity() -> None:
    policy = PolicySpec(name="resident", admission="fixed", reclamation="resident",
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
    policy = PolicySpec(name="resident", admission="fixed", reclamation="resident",
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
    policy = PolicySpec(name="resident", admission="fixed", reclamation="resident",
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


def test_existing_tool_progress_uses_hard_budget_at_high_or_when_idle() -> None:
    gib = 1024 ** 3
    used = [31 * gib]
    policy = PolicySpec(
        name="resident", admission="predicted", reclamation="resident",
        eviction="none", restore="none",
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=32 * 1024, progress_budget_mib=36 * 1024,
        emergency_free_mib=0, operation_headroom_mib=0,
        physical_sample=lambda: (used[0], 100 * gib),
    )
    coordinator.acquire("holder", 1 * 1024, 0, wait_class="tool_admission")
    with pytest.raises(AdmissionTimeout):
        coordinator.acquire("existing", 2 * 1024, 0, wait_class="tool_admission")
    coordinator.release("holder", 1 * 1024)
    # No Tool is active, so allow one existing session to cross HIGH and make
    # progress rather than deadlocking just below the watermark.
    coordinator.acquire("existing", 2 * 1024, 0, wait_class="tool_admission")
    coordinator.release("existing", 2 * 1024)
    used[0] = 33 * gib
    coordinator.acquire("existing", 2 * 1024, 0, wait_class="tool_admission")
    coordinator.release("existing", 2 * 1024)
    with pytest.raises(AdmissionTimeout):
        coordinator.acquire("existing", 4 * 1024, 0, wait_class="tool_admission")


def test_completion_releases_exactly_one_progress_waiter_below_high() -> None:
    """Reproduce the c16 boundary: idle at HIGH-minus-64MiB must not deadlock."""
    gib = 1024 ** 3
    mib = 1024 ** 2
    used = [31 * gib]
    policy = PolicySpec(
        name="resident", admission="fixed", reclamation="resident",
        eviction="none", restore="none",
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=32 * 1024, progress_budget_mib=36 * 1024,
        emergency_free_mib=0, operation_headroom_mib=0,
        physical_sample=lambda: (used[0], 100 * gib),
    )
    coordinator.acquire("finishing", 259, 1, wait_class="tool_admission")
    used[0] = 32 * gib - 64 * mib

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            coordinator.acquire, "next", 259, 2,
            wait_class="tool_admission",
        )
        deadline = time.monotonic() + 1
        while not coordinator._waiters and time.monotonic() < deadline:
            time.sleep(0.001)
        assert coordinator._waiters and not pending.done()

        coordinator.release("finishing", 259)
        pending.result(timeout=1)

    # The escape hatch admits one progress request, not the whole queue.
    with pytest.raises(AdmissionTimeout):
        coordinator.acquire("second", 259, 0, wait_class="tool_admission")
    coordinator.release("next", 259)


def test_high_hysteresis_blocks_only_new_session_creation() -> None:
    policy = PolicySpec(
        name="resident", admission="predicted", reclamation="resident",
        eviction="none", restore="none",
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=32, progress_budget_mib=36,
        emergency_free_mib=0, operation_headroom_mib=0,
    )
    coordinator.set_new_session_admission_blocked(True)
    with pytest.raises(AdmissionTimeout):
        coordinator.acquire("new", 1, 0, wait_class="create")
    coordinator.acquire("existing", 1, 0, wait_class="tool_admission")
    coordinator.release("existing", 1)
    coordinator.set_new_session_admission_blocked(False)
    coordinator.acquire("new", 1, 0, wait_class="create")
    coordinator.release("new", 1)


def test_new_pair_leaves_room_for_a_tool_to_make_progress() -> None:
    policy = PolicySpec(name="resident", admission="fixed", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                                    operation_headroom_mib=1, startup_headroom_mib=2,
                                    physical_sample=lambda: (2 * 1024**2, 100 * 1024**2))
    coordinator.register("running", Lifecycle())
    coordinator.register_runtime("running", Lifecycle())
    with pytest.raises(AdmissionTimeout):
        coordinator.materialize("new-pair", 6, lambda: pytest.fail("must not start"), 0)
    coordinator.acquire("running", 6, 0)
    coordinator.release("running", 6)


def test_wait_aware_pressure_includes_a_blocked_create_request() -> None:
    policy = PolicySpec(
        name="final", admission="predicted", reclamation="snapshot_pause",
        eviction="wait_aware_pressure", restore="reactive",
    )
    used = [2 * 1024**2]
    coordinator = PolicyCoordinator(
        policy, budget_mib=10, emergency_free_mib=0,
        operation_headroom_mib=1, startup_headroom_mib=2,
        physical_sample=lambda: (used[0], 100 * 1024**2),
    )
    coordinator.register("running", Lifecycle())
    coordinator.register_runtime("running", Lifecycle())
    created = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            coordinator.materialize, "new-pair", 6,
            lambda: created.set() or 0.01, 2,
        )
        deadline = time.monotonic() + 1
        while not coordinator._waiters and time.monotonic() < deadline:
            time.sleep(0.001)
        assert coordinator._waiters
        assert coordinator.pressure() is False
        assert coordinator.model_wait_plan(3.0) == (0.0, None)
        used[0] = 0
        pending.result(timeout=2)
    assert created.is_set()


@pytest.mark.parametrize('include_runtime', [True, False])
def test_high_watermark_proactively_evicts_a_safe_waiting_pair(include_runtime) -> None:
    policy = PolicySpec(
        name="final", admission="predicted", reclamation="snapshot_pause",
        eviction="wait_aware_pressure", restore="reactive",
        runtime_snapshot=include_runtime,
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=32, emergency_free_mib=0,
        operation_headroom_mib=0,
    )
    tool, runtime = Lifecycle(), Lifecycle()
    runtime_pauses = []
    coordinator.register("waiting", tool)
    coordinator.register_runtime(
        "waiting", runtime, before_pause=lambda: runtime_pauses.append(True),
    )
    coordinator.begin_model_wait("waiting", "wait-1", 10.0)

    assert coordinator.evict_one_for_watermark() is True
    assert (tool.pauses, runtime.pauses) == (1, int(include_runtime))
    assert runtime_pauses == ([True] if include_runtime else [])
    assert coordinator.pause_count == 1 + int(include_runtime)
    assert coordinator.evict_one_for_watermark() is False


def test_resident_policy_has_no_high_watermark_eviction_victim() -> None:
    policy = PolicySpec(
        name="resident", admission="predicted", reclamation="resident",
        eviction="none", restore="none",
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=32, emergency_free_mib=0,
        operation_headroom_mib=0,
    )
    coordinator.register("running", Lifecycle())
    assert coordinator.evict_one_for_watermark() is False


def test_wait_aware_admission_pauses_both_vms_during_model_wait() -> None:
    policy = PolicySpec(
        name="final", admission="predicted", reclamation="snapshot_pause",
        eviction="wait_aware_pressure", restore="reactive",
    )
    used = [9 * 1024**2]
    coordinator = PolicyCoordinator(
        policy, budget_mib=10, emergency_free_mib=0,
        operation_headroom_mib=0,
        physical_sample=lambda: (used[0], 100 * 1024**2),
    )
    tool = Lifecycle()
    runtime = Lifecycle()
    coordinator.register("waiting", tool)
    before_runtime_pause = []
    coordinator.register_runtime(
        "waiting", runtime,
        before_pause=lambda: before_runtime_pause.append(True),
    )
    coordinator.begin_model_wait("waiting", "request-1", 3.0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            coordinator.materialize, "new-pair", 4,
            lambda: 0.01, 2,
        )
        deadline = time.monotonic() + 1
        while (tool.pauses == 0 or runtime.pauses == 0) and time.monotonic() < deadline:
            time.sleep(0.001)
        assert tool.pauses == 1
        assert runtime.pauses == 1
        assert before_runtime_pause == [True]
        used[0] = 0
        pending.result(timeout=2)



def test_model_response_waits_for_claimed_pair_checkpoint() -> None:
    policy = PolicySpec(
        name="final", admission="predicted", reclamation="snapshot_pause",
        eviction="wait_aware_pressure", restore="reactive",
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=10, emergency_free_mib=0,
        operation_headroom_mib=0,
    )
    coordinator.register("waiting", Lifecycle())
    coordinator.register_runtime("waiting", Lifecycle())
    coordinator.begin_model_wait("waiting", "request-1", 3.0)
    victim = coordinator.victim_for_restore("other")
    assert victim is not None

    completed = threading.Event()
    thread = threading.Thread(
        target=lambda: (
            coordinator.complete_model_wait("waiting", "request-1"),
            completed.set(),
        )
    )
    thread.start()
    assert not completed.wait(timeout=0.05)
    coordinator.release_victim(victim)
    assert completed.wait(timeout=1)
    thread.join(timeout=1)


def test_first_pair_does_not_reserve_headroom_for_a_nonexistent_agent() -> None:
    policy = PolicySpec(name="resident", admission="fixed", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(policy, budget_mib=7, emergency_free_mib=0,
                                    operation_headroom_mib=1, startup_headroom_mib=2,
                                    physical_sample=lambda: (0, 100 * 1024**2))
    created = []
    coordinator.materialize("first-pair", 6, lambda: created.append(True) or 0.01, 0)
    assert created == [True]


def test_resident_policy_never_selects_or_pauses_a_victim() -> None:
    policy = PolicySpec(name="resident", admission="fixed", reclamation="resident",
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
    assert metrics["safety_intervention_count"] == 2
    assert metrics["safety_interventions_by_reason"] == {
        "configured_memory_budget": 1, "global_memory_budget": 1,
    }


def test_snapshot_policy_uses_only_idle_eligible_lru_victim() -> None:
    policy = PolicySpec(name="snapshot", admission="fixed", reclamation="snapshot_pause",
                        eviction="wait_aware_pressure", restore="reactive")
    coordinator = PolicyCoordinator(policy, budget_mib=1, emergency_free_mib=1,
                                    operation_headroom_mib=0,
                                    physical_sample=lambda: (2 * 1024**2, 100 * 1024**2))
    active, idle = Lifecycle(), Lifecycle()
    coordinator.register("active", active)
    coordinator.register("idle", idle)
    coordinator.set_tool_active("active", True)
    coordinator.begin_model_wait("idle", "wait-1", 10.0)
    victim = coordinator.victim_for_restore("requester")
    assert victim is not None and victim.session_id == "idle"
    coordinator.release_victim(victim)


def test_late_cleanup_activation_ignores_an_unregistered_session() -> None:
    policy = PolicySpec(name="resident", admission="off", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(policy, budget_mib=1, emergency_free_mib=0,
                                    operation_headroom_mib=0)
    coordinator.register("finished", Lifecycle())
    coordinator.unregister("finished")
    assert coordinator.set_tool_active_if_registered("finished", False) is False
    with pytest.raises(KeyError):
        coordinator.set_tool_active("finished", False)


def test_wait_aware_policy_never_selects_an_idle_non_waiting_session() -> None:
    policy = PolicySpec(
        name="final", admission="predicted", reclamation="snapshot_pause",
        eviction="wait_aware_pressure", restore="reactive",
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=1, emergency_free_mib=1,
        operation_headroom_mib=0,
    )
    lifecycle = Lifecycle()
    coordinator.register("idle", lifecycle)
    coordinator.set_eviction_eligible("idle", True)
    assert coordinator.victim_for_restore("requester") is None


def test_tool_admission_protects_resident_tool_before_memory_wait() -> None:
    """A delayed admission cannot be selected for pause by another waiter."""
    policy = PolicySpec(name="snapshot", admission="fixed",
                        reclamation="snapshot_pause", eviction="wait_aware_pressure",
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
    coordinator.begin_model_wait("tool", "wait-1", 10.0)
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
    policy = PolicySpec(name="snapshot", admission="fixed",
                        reclamation="snapshot_pause", eviction="wait_aware_pressure",
                        restore="reactive")
    coordinator = PolicyCoordinator(policy, budget_mib=1, emergency_free_mib=1,
                                    operation_headroom_mib=0)
    lifecycle = Lifecycle()
    coordinator.register("tool", lifecycle)
    coordinator.begin_model_wait("tool", "wait-1", 10.0)
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


def test_memory_return_excludes_tool_claim_and_new_admission() -> None:
    policy = PolicySpec(name="snapshot", admission="fixed",
                        reclamation="snapshot_pause", eviction="wait_aware_pressure",
                        restore="reactive")
    coordinator = PolicyCoordinator(
        policy, budget_mib=10, emergency_free_mib=0,
        operation_headroom_mib=0,
    )
    lifecycle = Lifecycle()
    coordinator.register("tool", lifecycle)

    coordinator.begin_tool_admission("tool", 1, 0)
    assert not coordinator.claim_memory_return("tool")
    coordinator.release("tool", 1)
    coordinator.set_tool_active("tool", False)
    assert coordinator.claim_memory_return("tool")

    attempting = threading.Event()
    activated = threading.Event()

    def activate() -> None:
        attempting.set()
        coordinator.set_tool_active("tool", True)
        activated.set()

    thread = threading.Thread(target=activate)
    thread.start()
    assert attempting.wait(timeout=1)
    assert not activated.wait(timeout=0.05)
    coordinator.release_memory_return("tool")
    thread.join(timeout=1)
    assert activated.is_set()


def test_admission_is_fifo_and_exports_overhead_metrics() -> None:
    policy = PolicySpec(name="resident", admission="fixed", reclamation="resident",
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
    assert metrics["discipline"] == "runnable_progress_before_create_fifo"
    assert metrics["admission_count"] == 3
    assert metrics["max_queue_depth"] == 2
    assert metrics["wait_p95_seconds"] is not None


def test_lifecycle_reservations_do_not_pollute_tool_admission_metrics() -> None:
    policy = PolicySpec(name="resident", admission="fixed", reclamation="resident",
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
    policy = PolicySpec(name="resident", admission="fixed", reclamation="resident",
                        eviction="none", restore="none")
    coordinator = PolicyCoordinator(
        policy, budget_mib=10, emergency_free_mib=1, operation_headroom_mib=0,
        physical_sample=lambda: (8 * 1024**2, 100 * 1024**2),
    )
    with pytest.raises(AdmissionTimeout):
        coordinator.acquire("tool", 3, 0)


def test_wait_plan_uses_only_request_time_prediction() -> None:
    policy = PolicySpec(
        name="predicted", admission="fixed", reclamation="snapshot_pause",
        eviction="wait_aware_pressure", restore="reactive",
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=1, emergency_free_mib=1, operation_headroom_mib=0,
        physical_sample=lambda: (2 * 1024**2, 100 * 1024**2),
    )
    # A missing estimate cannot be replaced with the held-out actual duration.
    assert coordinator.model_wait_plan(None) == (None, None)
    assert coordinator.model_wait_plan(3.0) == (0.0, None)


@pytest.mark.parametrize("eviction", ["eager", "fixed_delay", "time_oracle", "tiered_time_oracle", "tiered_lru_oracle"])
def test_unsupported_evictions_fail_before_execution(eviction):
    with pytest.raises(ValueError, match="supported eviction"):
        PolicySpec(name="unsupported", admission="fixed", reclamation="snapshot_pause",
                   eviction=eviction, restore="reactive")


def test_unsupported_proactive_restore_fails_before_execution():
    with pytest.raises(ValueError, match="supported restore"):
        PolicySpec(name="unsupported", admission="fixed", reclamation="snapshot_pause",
                   eviction="wait_aware_pressure", restore="proactive")
