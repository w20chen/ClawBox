import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from clawbox.experiments.admission import DemandLedger
from clawbox.experiments.policy import PolicyCoordinator, AdmissionTimeout
from clawbox.experiments.baselines import resolve_baseline

MIB = 1024**2


def test_tool_binding_does_not_block_other_sessions_or_revive_replaced_claim():
    c = PolicyCoordinator(resolve_baseline('predicted-resident').as_policy(), budget_mib=100,
                          emergency_free_mib=0, operation_headroom_mib=0)
    reading, finish = threading.Event(), threading.Event()
    def sample():
        reading.set()
        assert finish.wait(2)
        return 20*MIB, 20*MIB
    c.register_memory('s', tool=sample, lifecycle=lambda: (0, 0))
    c.acquire('s', 10, .1)
    original = c.demand.claims['s', 'tool_admission']
    with ThreadPoolExecutor(2) as workers:
        binding = workers.submit(c.bind_tool_memory, 's')
        assert reading.wait(1)
        try:
            workers.submit(c.acquire, 'other', 10, .1).result(timeout=1)
            with c._condition:
                assert c.demand.pending() == 20*MIB
            c.release('s', 10)
            c.acquire('s', 10, .1)
            replacement = c.demand.claims['s', 'tool_admission']
        finally:
            finish.set()
        binding.result(timeout=1)
    assert replacement is not original
    assert replacement.sample is None
    assert replacement.baseline == (0, 0)


def test_same_vm_inflight_waiter_does_not_block_another_vm():
    c = PolicyCoordinator(resolve_baseline('fixed-resident').as_policy(), budget_mib=100,
                          emergency_free_mib=0, operation_headroom_mib=0)
    c.acquire('busy', 10, .1)
    entered = threading.Event()
    def same_vm():
        entered.set()
        return c.acquire('busy', 10, 2)
    with ThreadPoolExecutor(2) as workers:
        pending = workers.submit(same_vm)
        assert entered.wait(.5)
        with c._condition:
            assert c._condition.wait_for(lambda: bool(c._waiters), .5)
            assert c.resource_feedback()['memory_blocked'] is False
        try:
            unrelated = workers.submit(c.acquire, 'other', 10, .2)
            unrelated.result(timeout=.5)
            assert not pending.done()  # One in-flight operation per VM still holds.
            c.release('other', 10)
        finally:
            c.release('busy', 10)
        pending.result(timeout=.5)
    c.release('busy', 10)


def test_realized_growth_is_not_reserved_twice_and_retained_pages_still_count():
    used = [100, 10]
    group = [10, 10]
    ledger = DemandLedger(sample=lambda: (used[0], 10000), capacity=1000)
    with ledger.condition:
        ledger.add(('a', 'tool'), scope='node-a', amount=500, local_amount=500,
                   sample=lambda: tuple(group))
        assert ledger.pending() == 500
        used[0] += 300; group[:] = [310, 310]
        assert ledger.pending() == 200
        assert not ledger.reasons(400)
        assert ledger.reasons(401) == ('global_memory_budget',)
        ledger.release(('a', 'tool'))
        assert used[0] == 400 and ledger.pending() == 0
        assert ledger.reasons(601)


def test_policy_skips_per_claim_samples_when_nominal_upper_bound_fits():
    calls = [0]
    def claim_sample():
        calls[0] += 1
        return 0, 0
    ledger = DemandLedger(sample=lambda: (0, 1000*MIB), capacity=1000*MIB)
    coordinator = PolicyCoordinator(
        resolve_baseline('predicted-resident').as_policy(), budget_mib=1000,
        emergency_free_mib=0, operation_headroom_mib=0,
        demand_ledger=ledger,
    )
    with ledger.condition:
        for index in range(160):
            ledger.add((str(index), 'tool_admission'), scope='local',
                       amount=MIB, local_amount=MIB, sample=claim_sample)
        assert coordinator._pressure_reasons(MIB) == ()
        assert not coordinator.local_commitment_exceeds(1,1000*MIB)
    assert calls[0] == 160  # add() establishes each claim baseline once.
    assert ledger.pending_upper_bound_fast_path_count == 2
    assert ledger.pending_exact_count == 0


def test_policy_uses_exact_claim_growth_when_upper_bound_nears_capacity():
    calls = [0]
    group = [0, 0]
    def claim_sample():
        calls[0] += 1
        return tuple(group)
    ledger = DemandLedger(sample=lambda: (5*MIB, 1000*MIB), capacity=10*MIB)
    coordinator = PolicyCoordinator(
        resolve_baseline('predicted-resident').as_policy(), budget_mib=10,
        emergency_free_mib=0, operation_headroom_mib=0,
        demand_ledger=ledger, physical_sample=ledger.sample,
    )
    with ledger.condition:
        ledger.add(('a', 'tool_admission'), scope='local',
                   amount=6*MIB, local_amount=6*MIB, sample=claim_sample)
        group[:] = [6*MIB, 6*MIB]
        assert coordinator._pressure_reasons(4*MIB) == ()
        assert not coordinator.local_commitment_exceeds(4,10*MIB)
        assert coordinator.local_commitment_exceeds(6,10*MIB)
    assert calls[0] == 5  # baseline, local/global checks, and two exact borrow guards.
    assert ledger.pending_upper_bound_fast_path_count == 0
    assert ledger.pending_exact_count == 4


def test_global_claims_are_shared_but_local_claims_do_not_spend_other_nodes():
    ledger = DemandLedger(sample=lambda: (200, 10000), capacity=1000)
    with ledger.condition:
        ledger.add(('a', 'tool'), scope='node-a', amount=500, local_amount=500)
        ledger.add(('b', 'tool'), scope='node-b', amount=100, local_amount=0)
        assert ledger.pending('node-a') == 500
        assert ledger.pending('node-b') == 0
        assert ledger.pending() == 600
        assert ledger.reasons(201)


def test_off_bypasses_tool_gate_but_not_lifecycle_accounting():
    c = PolicyCoordinator(resolve_baseline('uncontrolled-resident').as_policy(), budget_mib=10,
                          emergency_free_mib=0, operation_headroom_mib=0,
                          physical_sample=lambda: (10*MIB, 100*MIB))
    c.acquire('off', 0, .01)
    assert not c.demand.claims
    with pytest.raises(AdmissionTimeout):
        c.acquire('create', 1, .01, wait_class='create')
    assert not c.demand.claims


def test_two_nodes_cannot_race_past_global_budget():
    ledger = DemandLedger(sample=lambda: (0, 1000*MIB), capacity=10*MIB)
    coords = [PolicyCoordinator(resolve_baseline('fixed-resident').as_policy(), budget_mib=10,
                                emergency_free_mib=0, operation_headroom_mib=0,
                                demand_ledger=ledger, demand_scope=f'n{i}') for i in range(2)]
    barrier = threading.Barrier(2)
    def admit(i):
        barrier.wait()
        try:
            coords[i].acquire(str(i), 6, .05)
            return True
        except AdmissionTimeout:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(admit, range(2))) == [False, True]
    assert len(ledger.claims) == 1


def test_feedback_zero_deficit_uses_safe_nominal_bound(monkeypatch):
    from clawbox.experiments.policy import AdmissionRequest
    c=PolicyCoordinator(resolve_baseline('fixed-resident').as_policy(), budget_mib=100,
                        emergency_free_mib=0, operation_headroom_mib=0)
    with c._condition:
        c.demand.add(('old','create'),scope=c.demand_scope,amount=MIB,local_amount=MIB)
        ticket=object()
        c._waiters.append(ticket)
        c._waiter_requests[ticket]=AdmissionRequest('new',MIB,'create',True)
        monkeypatch.setattr(c.demand,'pending',lambda *args:pytest.fail('unneeded exact read'))
        assert c.resource_feedback()['deficit_bytes']==0


def test_restore_base_is_not_mistaken_for_tool_execution_growth():
    c = PolicyCoordinator(resolve_baseline('predicted-resident').as_policy(), budget_mib=100,
                          emergency_free_mib=0, operation_headroom_mib=0)
    group = [0, 0]
    c.register_memory('s', tool=lambda: tuple(group), lifecycle=lambda: tuple(group))
    c.acquire('s', 10, .1)
    c.acquire('s', 40, .1, wait_class='restore')
    group[:] = [40*MIB, 40*MIB]
    assert c.demand.pending() == 10*MIB
    c.release('s', 40, wait_class='restore')
    c.bind_tool_memory('s')
    group[:] = [46*MIB, 46*MIB]
    assert c.demand.pending() == 4*MIB
    c.release('s', 10)
    assert not c.demand.claims


def test_severe_pressure_uses_sustained_stall_deltas_and_hysteresis(tmp_path, monkeypatch):
    from clawbox.experiments import admission
    now = [0.0]; used = [950]
    monkeypatch.setattr(admission.time, 'monotonic', lambda: now[0])
    (tmp_path / 'memory.stat').write_text('pgscan 0\npgsteal 0\n')
    psi = tmp_path / 'memory.pressure'
    psi.write_text('some avg10=0 total=0\n')
    ledger = DemandLedger(sample=lambda: (used[0], 1000), capacity=1000, cgroup=tmp_path)
    with ledger.condition:
        assert not ledger.reasons(0)
        now[0] = .2; psi.write_text('some avg10=0 total=40000\n')
        assert ledger.reasons(0) == ('sustained_memory_congestion',)
        now[0] = .4; used[0] = 850
        assert ledger.reasons(0) == ('sustained_memory_congestion',)
        now[0] = .6; used[0] = 799
        assert not ledger.reasons(0)


def test_controller_failure_propagates_through_admission():
    ledger = DemandLedger(sample=lambda: (0, 100*MIB), capacity=10*MIB)
    def fail():
        raise RuntimeError('controller stopped')
    ledger.check = fail
    c = PolicyCoordinator(resolve_baseline('fixed-resident').as_policy(), budget_mib=10,
        emergency_free_mib=0, operation_headroom_mib=0, demand_ledger=ledger)
    with pytest.raises(RuntimeError, match='controller stopped'):
        c.acquire('s', 1, .1)
    assert not ledger.claims


def test_no_psi_reclaim_signal_recovers_without_assuming_guest_backing_is_freed(tmp_path, monkeypatch):
    from clawbox.experiments import admission
    now = [0.0]
    monkeypatch.setattr(admission.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(admission.os, 'sysconf', lambda _: 4096, raising=False)
    stats = tmp_path / 'memory.stat'
    stats.write_text('pgscan 0\npgsteal 0\n')
    ledger = DemandLedger(sample=lambda: (950, 1000), capacity=1000, cgroup=tmp_path)
    with ledger.condition:
        assert not ledger.reasons(0)
        now[0] = .2; stats.write_text('pgscan 4000\npgsteal 20\n')
        assert 'sustained_memory_congestion' in ledger.reasons(0)
        now[0] = .5
        assert 'sustained_memory_congestion' in ledger.reasons(0)
        for stamp in (.51, .52, .53, .54):
            now[0] = stamp
            assert 'sustained_memory_congestion' in ledger.reasons(0)
        now[0] = 1.3
        assert not ledger.reasons(40)  # retained charge unchanged; predicted demand still fits
        assert ledger.reasons(51) == ('global_memory_budget',)
