from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_policy_events_coalesce_to_running_and_one_trailing_scan():
    from threading import Event
    from clawbox.experiments.policy import PolicyEventExecutor

    entered, release = Event(), Event()
    calls = []
    executor = PolicyEventExecutor(workers=4)

    def scan(value):
        calls.append(value)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)

    first = executor.submit_coalesced("pressure", scan, 0)
    assert entered.wait(1)
    for value in range(1, 101):
        assert executor.submit_coalesced("pressure", scan, value) is first
    release.set()
    first.result(timeout=2)
    assert calls == [0, 100]
    assert executor.metrics() == {
        "submitted": 101, "coalesced": 100, "executed": 2,
        "active_keys": 0, "failures": 0,
    }
    executor.close()


def test_coalesced_policy_event_failure_is_reported_to_arm_health():
    from clawbox.experiments.policy import PolicyEventExecutor

    executor = PolicyEventExecutor(workers=1)
    done = executor.submit_coalesced(
        "pressure", lambda: (_ for _ in ()).throw(ValueError("broken pressure")),
    )
    done.result(timeout=1)
    with pytest.raises(RuntimeError, match="coalesced policy event failed") as raised:
        executor.check()
    assert isinstance(raised.value.__cause__, ValueError)
    executor.close()

from clawbox.experiments.policy import AdmissionTimeout, PolicyCoordinator
from clawbox.experiments.spec import PolicySpec
from clawbox.experiments.spec_types import SnapshotTier


def test_placement_retry_timeout_does_not_cancel_a_queued_fifo_ticket():
    from clawbox.experiments.policy import AdmissionRequest
    policy = PolicySpec(name='resident', admission='fixed', reclamation='resident',
                        eviction='none', restore='none')
    c = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                          operation_headroom_mib=0,
                          physical_sample=lambda: (0, 100*1024**2))
    with c._condition:
        c._waiters.append('older')
        c._waiter_requests['older'] = AdmissionRequest('older', 1024**2, 'tool_admission', False)
        c._progress_waiters.add('older')
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(c.acquire, 'new', 1, 2, wait_class='create',
                              blocked_head_timeout_s=.02)
        time.sleep(.25)
        assert not waiting.done()
        with c._condition:
            c._waiters.remove('older')
            c._waiter_requests.pop('older')
            c._progress_waiters.remove('older')
            c._signal_admission_waiters_locked()
            c._condition.notify_all()
        assert waiting.result(timeout=1) >= .2
    assert ('new', 'create') in c.demand.claims


def test_placement_reconsiders_a_genuinely_memory_blocked_head():
    policy = PolicySpec(name='resident', admission='fixed', reclamation='resident',
                        eviction='none', restore='none')
    c = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                          operation_headroom_mib=0,
                          physical_sample=lambda: (9*1024**2, 100*1024**2))
    with pytest.raises(AdmissionTimeout, match='placement head remains blocked'):
        c.acquire('new', 4, 2, wait_class='create', blocked_head_timeout_s=.02)
    assert not c._waiters
    assert not c.demand.claims


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


@pytest.mark.parametrize('used_mib, expected', [(0, 'create'), (8, 'tool')])
def test_progress_bypasses_only_memory_blocked_creation(used_mib, expected):
    from clawbox.experiments.policy import AdmissionRequest
    policy = PolicySpec(name='resident', admission='fixed', reclamation='resident',
                        eviction='none', restore='none')
    c = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                          operation_headroom_mib=0,
                          physical_sample=lambda: (used_mib*1024**2, 100*1024**2))
    with c._condition:
        c._waiters.extend(['create', 'tool'])
        c._waiter_requests['create'] = AdmissionRequest('new', 4*1024**2, 'create', False)
        c._waiter_requests['tool'] = AdmissionRequest('running', 1024**2, 'tool_admission', False)
        c._progress_waiters.add('tool')
        assert c._admission_head_locked() == expected


def test_resource_feedback_reselects_head_removed_during_exact_sampling():
    """Exact cgroup reads may release admission while the head completes."""
    from clawbox.experiments.policy import AdmissionRequest, MIB
    policy = PolicySpec(name='resident', admission='fixed', reclamation='resident',
                        eviction='none', restore='none')
    c = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                          operation_headroom_mib=0,
                          physical_sample=lambda: (9*MIB, 100*MIB))
    create = object()
    tool = object()
    with c._condition:
        c._waiters.extend((create, tool))
        c._waiter_requests[create] = AdmissionRequest(
            'create', 4*MIB, 'create', False,
        )
        c._waiter_requests[tool] = AdmissionRequest(
            'tool', MIB, 'tool_admission', False,
        )
        c._progress_waiters.add(tool)

    removed = False

    original_observation = c.demand.pending_observation

    def sample_while_head_completes(*, progress=False):
        nonlocal removed
        measured = original_observation(progress=progress)
        with c._condition:
            if not removed:
                c._waiters.remove(create)
                c._waiter_requests.pop(create)
                removed = True
        return measured

    c.demand.pending_observation = sample_while_head_completes

    # Ensure this fixture reaches the exact path. With no pending claims the
    # nominal upper bound already fits and the sampler is correctly skipped.
    with c._condition:
        c.demand.add(('active', 'tool_admission'), scope=c.demand_scope,
                     amount=MIB, local_amount=MIB)
        c.demand.claims['active', 'tool_admission'].sample = lambda: (MIB, MIB)

    feedback = c.resource_feedback()
    assert removed
    assert feedback['waiters'] == 1
    assert feedback['reasons'] == []


def test_feasible_tool_prefix_does_not_wait_for_each_predecessor_thread():
    from clawbox.experiments.policy import AdmissionRequest, MIB
    policy = PolicySpec(name='resident', admission='fixed', reclamation='resident',
                        eviction='none', restore='none')
    c = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                          operation_headroom_mib=0,
                          physical_sample=lambda: (0, 100*MIB))
    older = object()
    with c._condition:
        c._waiters.append(older)
        c._waiter_requests[older] = AdmissionRequest(
            'older', MIB, 'tool_admission', False, total_bytes=MIB,
        )
        c._progress_waiters.add(older)

    # The older request's callback thread has not run yet.  Both conservative
    # charges fit, so this later thread may commit its own atomic claim instead
    # of waiting for a notify-all/FIFO scheduling round.
    assert c.acquire('new', 1, .1) >= 0
    assert ('new', 'tool_admission') in c.demand.claims

    c.release('new', 1)
    with c._condition:
        c._waiters.remove(older)
        c._waiter_requests.pop(older)
        c._progress_waiters.remove(older)


def test_waiting_admission_refreshes_one_snapshot_before_locked_recheck():
    """A wake-up must not sample and re-enter admission once per resource."""
    from clawbox.experiments.policy import AdmissionRequest, MIB
    policy = PolicySpec(name='resident', admission='fixed', reclamation='resident',
                        eviction='none', restore='none')
    c = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                          operation_headroom_mib=0,
                          physical_sample=lambda: (0, 100*MIB))
    older = object()
    with c._condition:
        c._waiters.append(older)
        c._waiter_requests[older] = AdmissionRequest(
            'older', MIB, 'create', False, total_bytes=MIB,
        )

    # Once acquire has waited, any legacy locked fallback would call this for
    # LOCAL and global observations separately.  The refreshed snapshot path
    # samples outside the condition and carries both values into the decision.
    c.demand.sample_without_condition = (
        lambda *args, **kwargs: pytest.fail('wake-up used locked sample fallback')
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(c.acquire, 'new', 1, 1)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with c._condition:
                if len(c._waiters) == 2:
                    c._waiters.remove(older)
                    c._waiter_requests.pop(older)
                    c._signal_admission_waiters_locked()
                    c._condition.notify_all()
                    break
            time.sleep(.001)
        assert waiting.result(timeout=1) >= 0

    assert ('new', 'tool_admission') in c.demand.claims


def test_feasible_progress_prefix_is_bounded_to_wake_batch():
    from clawbox.experiments.policy import AdmissionRequest, MIB
    policy = PolicySpec(name='resident', admission='fixed', reclamation='resident',
                        eviction='none', restore='none')
    c = PolicyCoordinator(policy, budget_mib=100, emergency_free_mib=0,
                          operation_headroom_mib=0,
                          physical_sample=lambda: (0, 100*MIB))
    tickets = [object() for _ in range(c._PROGRESS_WAKE_BATCH + 1)]
    with c._condition:
        for index, ticket in enumerate(tickets):
            c._waiters.append(ticket)
            c._waiter_requests[ticket] = AdmissionRequest(
                str(index), MIB, 'tool_admission', False, total_bytes=MIB,
            )
            c._progress_waiters.add(ticket)
        head = tickets[0]
        assert c._feasible_progress_prefix_locked(tickets[7], head) == (8*MIB, 8*MIB)
        assert c._feasible_progress_prefix_locked(tickets[8], head) is None


def test_impossible_tool_prefix_does_not_scan_existing_vm_claims():
    from clawbox.experiments.policy import AdmissionRequest, MIB
    policy = PolicySpec(name='resident', admission='fixed', reclamation='resident',
                        eviction='none', restore='none')
    c = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                          operation_headroom_mib=0,
                          physical_sample=lambda: (8*MIB, 100*MIB))
    older = object()
    with c._condition:
        c._waiters.append(older)
        c._waiter_requests[older] = AdmissionRequest('older', MIB, 'tool_admission', False)
        c._progress_waiters.add(older)
        c.demand.add(('active', 'tool_admission'), scope=c.demand_scope,
                     amount=MIB, local_amount=MIB)
        c.demand.claims['active', 'tool_admission'].sample = (
            lambda: pytest.fail('an impossible prefix scanned VM memory'))
    with pytest.raises(AdmissionTimeout):
        c.acquire('new', 4, .02)
    assert c.demand.pending_exact_count == 0
    assert ('new', 'tool_admission') not in c.demand.claims


def test_pressure_admission_prefetches_exact_claims_before_lock_commit():
    mib = 1024**2
    policy = PolicySpec(name='resident', admission='fixed', reclamation='resident',
                        eviction='none', restore='none')
    c = PolicyCoordinator(
        policy, budget_mib=10, emergency_free_mib=0,
        operation_headroom_mib=0,
        physical_sample=lambda: (8 * mib, 100 * mib),
    )
    materialized = [0]

    def claim_sample():
        return materialized[0], materialized[0]

    with c._condition:
        c.demand.add(
            ('active', 'tool_admission'), scope=c.demand_scope,
            amount=2 * mib, local_amount=2 * mib, sample=claim_sample,
        )
    materialized[0] = 2 * mib
    c.demand.pending_breakdown_without_condition = (
        lambda _scope: pytest.fail('exact sampling rejoined the admission lock')
    )

    c.acquire('new', 2, .1)

    assert ('new', 'tool_admission') in c.demand.claims
    assert c.demand.pending_exact_count == 1


def test_tool_prefix_stays_fifo_when_the_whole_prefix_cannot_fit():
    from clawbox.experiments.policy import AdmissionRequest, MIB
    policy = PolicySpec(name='resident', admission='fixed', reclamation='resident',
                        eviction='none', restore='none')
    c = PolicyCoordinator(policy, budget_mib=10, emergency_free_mib=0,
                          operation_headroom_mib=0,
                          physical_sample=lambda: (0, 100*MIB))
    older = object()
    with c._condition:
        c._waiters.append(older)
        c._waiter_requests[older] = AdmissionRequest(
            'older', 0, 'tool_admission', False, total_bytes=9*MIB,
        )
        c._progress_waiters.add(older)

    with pytest.raises(AdmissionTimeout):
        c.acquire('new', 0, .02, total_mib=2)
    assert ('new', 'tool_admission') not in c.demand.claims

    with c._condition:
        c._waiters.remove(older)
        c._waiter_requests.pop(older)
        c._progress_waiters.remove(older)


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
    coordinator.release("restore", 4, wait_class="restore")


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
        coordinator.release("restore", 4, wait_class="restore")
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
    coordinator.release("new", 1, wait_class="create")


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
        assert coordinator.blocked_demand_pressure()
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
    coordinator.begin_model_wait("waiting", "wait-1")

    assert coordinator.evict_one_for_watermark() is True
    assert (tool.pauses, runtime.pauses) == (1, int(include_runtime))
    assert runtime_pauses == ([True] if include_runtime else [])
    assert coordinator.pause_count == 1 + int(include_runtime)
    assert coordinator.evict_one_for_watermark() is False


def test_actual_model_response_removes_unpredicted_idle_candidate() -> None:
    policy = PolicySpec(
        name="wait", admission="predicted", reclamation="snapshot_pause",
        eviction="wait_aware_pressure", restore="reactive",
    )
    coordinator = PolicyCoordinator(policy, budget_mib=32, emergency_free_mib=0,
                                    operation_headroom_mib=0)
    tool = Lifecycle()
    coordinator.register("waiting", tool)
    coordinator.begin_model_wait("waiting", "request")
    coordinator.set_tool_active("waiting", True)
    assert coordinator.victim_for_restore("other") is None
    coordinator.set_tool_active("waiting", False)
    assert coordinator.watermark_reclaim_candidate() is not None
    coordinator.complete_model_wait("waiting", "request")
    assert coordinator.evict_one_for_watermark() is False
    assert tool.pauses == 0


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
    coordinator.begin_model_wait("waiting", "request-1")
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
    coordinator.begin_model_wait("waiting", "request-1")
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
    coordinator.begin_model_wait("idle", "wait-1")
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
    coordinator.begin_model_wait("tool", "wait-1")
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
    coordinator.begin_model_wait("tool", "wait-1")
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
    assert metrics["discipline"] == (
        "feasible_fifo_progress_with_aggregate_per_vm_tool_claims"
    )
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


def test_wait_plan_needs_pressure_but_not_duration_prediction() -> None:
    policy = PolicySpec(
        name="predicted", admission="fixed", reclamation="snapshot_pause",
        eviction="wait_aware_pressure", restore="reactive",
    )
    coordinator = PolicyCoordinator(
        policy, budget_mib=1, emergency_free_mib=1, operation_headroom_mib=0,
        physical_sample=lambda: (2 * 1024**2, 100 * 1024**2),
    )
    assert coordinator.blocked_demand_pressure()
    assert coordinator.blocked_demand_pressure()


@pytest.mark.parametrize("eviction", ["eager", "fixed_delay", "time_oracle", "tiered_time_oracle", "tiered_lru_oracle"])
def test_unsupported_evictions_fail_before_execution(eviction):
    with pytest.raises(ValueError, match="supported eviction"):
        PolicySpec(name="unsupported", admission="fixed", reclamation="snapshot_pause",
                   eviction=eviction, restore="reactive")


def test_unsupported_proactive_restore_fails_before_execution():
    with pytest.raises(ValueError, match="supported restore"):
        PolicySpec(name="unsupported", admission="fixed", reclamation="snapshot_pause",
                   eviction="wait_aware_pressure", restore="proactive")


def idle_pair_coordinator():
    c = PolicyCoordinator(PolicySpec(name="wait", admission="off", reclamation="snapshot_pause",
        eviction="wait_aware_pressure", restore="reactive"), budget_mib=32,
        emergency_free_mib=0, operation_headroom_mib=0)
    tool, runtime = Lifecycle(), Lifecycle()
    c.register("idle", tool); c.register_runtime("idle", runtime)
    c.begin_model_wait("idle", "request")
    return c, tool, runtime


def test_queue_snapshot_includes_lock_local_session_activity() -> None:
    c = PolicyCoordinator(
        PolicySpec(name="wait", admission="off", reclamation="snapshot_pause",
                   eviction="wait_aware_pressure", restore="reactive"),
        budget_mib=32, emergency_free_mib=0, operation_headroom_mib=0,
    )
    c.register("model", Lifecycle())
    c.register_runtime("model", Lifecycle())
    c.begin_model_wait("model", "request-1")
    c.register("tool", Lifecycle())
    c.set_tool_active("tool", True)

    snapshot = c.queue_snapshot()

    assert snapshot["registered_sessions"] == 2
    assert snapshot["model_waiting_sessions"] == 1
    assert snapshot["tool_active_sessions"] == 1
    assert snapshot["eviction_in_progress_sessions"] == 0


def test_restore_409_recovery_honors_pair_scope():
    c, tool, runtime = idle_pair_coordinator()
    calls = []
    class CapacityRejected(RuntimeError):
        status_code = 409
    def restore():
        calls.append(True)
        if len(calls) == 1:
            raise CapacityRejected()
        return 0.01
    c.restore("other", 0, restore, 1)
    assert len(calls) == 2
    assert tool.pauses == runtime.pauses == 1
    assert c.pause_count == 2


def test_response_during_tool_pause_still_finishes_committed_runtime_pause():
    c, tool, runtime = idle_pair_coordinator()
    entered, finish = threading.Event(), threading.Event()
    original = tool.checkpoint_and_evict
    def pause():
        entered.set()
        assert finish.wait(2)
        return original()
    tool.checkpoint_and_evict = pause
    with ThreadPoolExecutor(max_workers=2) as executor:
        pause_future = executor.submit(c.evict_one_for_watermark)
        assert entered.wait(1)
        response = executor.submit(c.complete_model_wait, "idle", "request")
        deadline = time.monotonic() + 1
        while not c._sessions["idle"].response_ready and time.monotonic() < deadline:
            time.sleep(.001)
        assert c._sessions["idle"].response_ready
        assert not response.done()
        finish.set()
        assert pause_future.result(timeout=2)
        response.result(timeout=2)
    assert tool.pauses == runtime.pauses == 1
    assert c.watermark_reclaim_candidate() is None


def test_admission_uses_configured_selector_instead_of_direct_checkpoint():
    c, tool, runtime = idle_pair_coordinator()
    calls = []
    c.pressure_relief = lambda: calls.append(True) or False
    c.physical_sample = lambda: (64 * 1024**2, 1024 * 1024**2)
    with pytest.raises(AdmissionTimeout):
        c.acquire("other", 1, 0)
    assert calls
    assert tool.pauses == runtime.pauses == 0


@pytest.mark.parametrize("runtime", ["pressure_lifecycle", "adaptive_joint"])
def test_llm_prediction_dependent_runtime_modes_are_rejected(runtime):
    with pytest.raises(ValueError, match="LLM-duration prediction is not implemented"):
        PolicySpec(name="invalid", admission="off", reclamation="snapshot_pause",
                   eviction="wait_aware_pressure", restore="reactive", runtime_control=runtime)
