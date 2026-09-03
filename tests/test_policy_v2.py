from __future__ import annotations

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
