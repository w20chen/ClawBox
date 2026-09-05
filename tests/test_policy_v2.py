from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from clawbox.experiments.policy import AdmissionTimeout, PolicyCoordinator
from clawbox.experiments.spec import PolicySpec


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
    assert coordinator.victim_for_restore("requester").session_id == "idle"


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
    assert coordinator.victim_for_restore("other").session_id == "tool"


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
    assert metrics["discipline"] == "fifo"
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
