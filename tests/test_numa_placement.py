from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
import time

import pytest

from clawbox.experiments.admission import DemandLedger
from clawbox.experiments.baselines import resolve_baseline
from clawbox.experiments.numa_placement import CpuMonitor, SessionPlacement
from clawbox.experiments.policy import PolicyCoordinator, AdmissionTimeout
from clawbox.experiments.spec import ExecutionSpec, ExperimentSpec
from clawbox.experiments.configure import configure_experiment
from test_supernode import nodes

MIB=1024**2


def scheduler(*, used=(0,0), cpu=(0.,0.), overcommit=1, capacity=20):
    ledger=DemandLedger(sample=lambda:(sum(used)*MIB,100*MIB),capacity=capacity*MIB)
    domains={}
    for i in range(2):
        coordinator=PolicyCoordinator(resolve_baseline('fixed-resident').as_policy(),
            budget_mib=10,emergency_free_mib=0,operation_headroom_mib=0,
            physical_sample=lambda i=i:(used[i]*MIB,100*MIB),
            demand_ledger=ledger,demand_scope=f'n{i}')
        domains[f'n{i}']=SimpleNamespace(coordinator=coordinator,cpus=f'{2*i}-{2*i+1}')
    monitor=SimpleNamespace(sample=lambda:{f'n{i}':{'cpu_utilization':cpu[i],
        'scheduling_pressure':None} for i in range(2)})
    events=[]
    return SessionPlacement(domains,cpu_overcommit=overcommit,monitor=monitor,emit=events.append),ledger,events


@pytest.mark.parametrize('wait_class,queued_mib,expected_blocked,queue_reason', [
    ('restore', 1, True, 'fifo_predecessor'),
    ('create', 1, True, 'fifo_predecessor'),
    ('create', 11, False, None),  # Existing progress bypasses blocked creation.
    ('tool_admission', 1, False, None),  # Feasible bounded progress prefix.
    ('tool_admission', 9, True, 'configured_memory_budget'),
])
def test_resume_candidate_observes_admission_predecessors_when_own_restore_fits(
    wait_class, queued_mib, expected_blocked, queue_reason,
):
    from clawbox.experiments.policy import AdmissionRequest
    from clawbox.experiments.session_binding import choose_resume
    placement, ledger, _ = scheduler()
    source = placement.domains['n0'].coordinator
    placement.suspended['session'] = ('n0', 2, 'template')
    ticket = object()
    with ledger.condition:
        source._waiters.append(ticket)
        source._waiter_requests[ticket] = AdmissionRequest('older',queued_mib*MIB,wait_class,False)
        if wait_class != 'create':
            source._progress_waiters.add(ticket)
        source._lifecycle_wait_samples['restore'].extend([8.,12.])
    before = (tuple(source._waiters), dict(source._waiter_requests), set(source._progress_waiters))
    candidates = placement.resume_candidates('session',memory_mib=2)
    home = candidates['n0']
    assert home['safety_reasons'] == []  # The individual restore fits available capacity.
    assert home['eligible'] is True
    assert home['blocked'] is expected_blocked
    assert home['restore_admission_queue']['blocked'] is expected_blocked
    assert home['restore_admission_queue']['reasons'] == ([queue_reason] if queue_reason else [])
    assert home['resource_wait_seconds'] == (10. if expected_blocked else 0.)
    assert candidates['n1']['resource_wait_seconds'] == 0.
    selected,_ = choose_resume('n0',candidates,policy='load-aware',threshold_seconds=0.)
    assert selected == ('n1' if expected_blocked else 'n0')
    assert (tuple(source._waiters),dict(source._waiter_requests),set(source._progress_waiters)) == before
    assert not ledger.claims
    assert not placement.sessions  # Selection observes; it does not acquire CPU or memory.


def test_resume_queue_observation_skips_duplicate_inflight_lifecycle_waiter():
    from clawbox.experiments.policy import AdmissionRequest
    placement,ledger,_ = scheduler()
    source = placement.domains['n0'].coordinator
    placement.suspended['session'] = ('n0',2,'template')
    ticket = object()
    with ledger.condition:
        source._waiters.append(ticket)
        source._waiter_requests[ticket] = AdmissionRequest('other',MIB,'restore',False)
        source._progress_waiters.add(ticket)
        ledger.add(('other','restore'),scope='n0',amount=MIB,local_amount=MIB)
    candidates = placement.resume_candidates('session',memory_mib=1)
    assert candidates['n0']['blocked'] is False
    assert candidates['n0']['restore_admission_queue']['queued_requests'] == 1
    assert len(ledger.claims) == 1


def test_resume_queue_batch_limit_has_unknown_wait_without_measured_history():
    from clawbox.experiments.policy import AdmissionRequest
    placement,ledger,_ = scheduler()
    source = placement.domains['n0'].coordinator
    source._PROGRESS_WAKE_BATCH = 1
    placement.suspended['session'] = ('n0',2,'template')
    ticket = object()
    with ledger.condition:
        source._waiters.append(ticket)
        source._waiter_requests[ticket] = AdmissionRequest('other',MIB,'tool_admission',False)
        source._progress_waiters.add(ticket)
    row = placement.resume_candidates('session',memory_mib=1)['n0']
    assert row['safety_reasons'] == []
    assert row['restore_admission_queue']['reasons'] == ['fifo_predecessor']
    assert row['blocked'] is True
    assert row['resource_wait_seconds'] is None
    assert row['resource_wait_source'] == 'unknown'


def test_reporting_does_not_probe_live_claims():
    s,ledger,_=scheduler()
    def slow_charge():
        pytest.fail('periodic reporting must reuse admission observations')
    with ledger.condition:
        ledger.add(('old','create'),scope='n0',amount=MIB,local_amount=MIB)
        ledger.claims['old','create'].sample=slow_charge
    report = s.sample()['n0']
    assert report['outstanding_bytes'] is None
    assert report['remaining_budget_bytes'] is None
    assert report['outstanding_nominal_upper_bound_bytes'] == MIB


def test_periodic_reporting_never_waits_behind_admission_lock():
    scheduler_state, ledger, _ = scheduler()
    entered, release = Event(), Event()

    def hold_admission():
        with ledger.condition:
            entered.set()
            assert release.wait(2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_admission)
        assert entered.wait(1)
        started = time.monotonic()
        row = scheduler_state.sample(blocking=False)
        assert time.monotonic() - started < .1
        assert all(value == {"measurement": "unavailable_admission_lock_busy"}
                   for value in row.values())
        release.set()
        holder.result()


def test_queue_snapshot_separates_requests_tasks_and_resident_vms():
    from clawbox.experiments.policy import AdmissionRequest
    s, ledger, _ = scheduler()
    c = s.domains['n0'].coordinator
    c.register('tool-task', SimpleNamespace(resident=True))
    c.register_runtime('tool-task', SimpleNamespace(resident=True))
    c.register('restore-task', SimpleNamespace(resident=False))
    c.register_runtime('restore-task', SimpleNamespace(resident=True))
    requests = [AdmissionRequest('new-task', MIB, 'create', False),
                AdmissionRequest('tool-task', MIB, 'tool_admission', False),
                AdmissionRequest('tool-task', MIB, 'tool_admission', False),
                AdmissionRequest('restore-task', MIB, 'restore', False)]
    with ledger.condition:
        for ticket, request in enumerate(requests):
            c._waiters.append(ticket)
            c._waiter_requests[ticket] = request
    row = s.sample()['n0']
    assert row['admission_queue_depth'] == 4
    assert row['admission_queue_depth_by_class'] == {'create': 1, 'tool_admission': 2, 'restore': 1}
    assert row['admission_queue_tasks'] == 3
    assert row['tool_admission_queue_tasks'] == 1
    assert row['admission_queue_resident_vms'] == 3
    assert row['admission_queue_oldest_seconds'] >= 0


def test_low_pressure_placement_does_not_sample_live_claims():
    s, ledger, events = scheduler()
    with ledger.condition:
        ledger.add(('old', 'tool_admission'), scope='n0', amount=MIB, local_amount=MIB)
        ledger.claims['old', 'tool_admission'].sample = lambda: pytest.fail(
            'placement must not read every live VM when nominal demand fits')
    assert s.acquire('new', vcpus=1, memory_mib=1, timeout=.2) in s.domains
    assert events[-1]['outstanding_bytes_kind'] == 'nominal_upper_bound'


@pytest.mark.parametrize('used,cpu', [((8,0),(0.,0.)),((0,0),(.9,.1))])
def test_load_aware_selects_lower_pressure_and_reserves_once(used,cpu):
    s,ledger,events=scheduler(used=used,cpu=cpu)
    assert s.acquire('a',vcpus=2,memory_mib=3,timeout=.1)=='n1'
    assert len(ledger.claims)==1 and ledger.pending()==3*MIB
    assert events[0]['compute_domain']=='n1'
    c=s.domains['n1'].coordinator
    charge=[0,0]
    c.register_memory('a',tool=lambda:tuple(charge),lifecycle=lambda:tuple(charge))
    charge[:]=[2*MIB,2*MIB]
    assert ledger.pending()==MIB
    c.release('a',3,wait_class='create');s.release('a')
    assert not ledger.claims and not s.sessions
    assert all(not d.coordinator._resident_capacity_claims for d in s.domains.values())


def test_template_affinity_only_breaks_balanced_verified_tie():
    s,ledger,events=scheduler()
    assert s.acquire('a',vcpus=1,memory_mib=1,timeout=.1,
        template_id='tpl-a',template_aware=True,
        template_sharing_supported=True)=='n0'
    # n1 is only slightly less loaded and remains inside the allowed balance
    # envelope, so verified template reuse may break the tie toward n0.
    assert s.acquire('b',vcpus=1,memory_mib=1,timeout=.1,
        template_id='tpl-a',template_aware=True,
        template_sharing_supported=True,balance_slack=.3)=='n0'
    assert events[-1]['template_affinity_applied'] is True
    for sid in ('a','b'):
        node=s.sessions[sid][0]
        s.domains[node].coordinator.release(sid,1,wait_class='create')
        s.release(sid)
    assert not ledger.claims


def test_unverified_template_sharing_has_no_affinity_bonus():
    s,ledger,events=scheduler()
    assert s.acquire('a',vcpus=1,memory_mib=1,timeout=.1,
        template_id='tpl-a',template_aware=True,
        template_sharing_supported=False)=='n0'
    assert s.acquire('b',vcpus=1,memory_mib=1,timeout=.1,
        template_id='tpl-a',template_aware=True,
        template_sharing_supported=False)=='n1'
    assert events[-1]['template_affinity_applied'] is False
    assert events[-1]['template_affinity_reason']=='sharing_not_verified'


def test_static_placement_does_not_silently_reselect():
    s,ledger,_=scheduler(used=(10,0))
    with pytest.raises(AdmissionTimeout):
        s.acquire('a',vcpus=2,memory_mib=1,timeout=.01,fixed='n0')
    assert not ledger.claims and not s.sessions


def test_cpu_checks_and_memory_reservations_are_atomic():
    s,ledger,_=scheduler()
    barrier=Barrier(3)
    def reserve(i):
        barrier.wait()
        try:return s.acquire(str(i),vcpus=2,memory_mib=3,timeout=.05)
        except AdmissionTimeout:return None
    with ThreadPoolExecutor(3) as pool:
        selected=list(pool.map(reserve,range(3)))
    assert sorted(x for x in selected if x)==['n0','n1']
    assert selected.count(None)==1
    assert ledger.pending()==6*MIB and len(ledger.claims)==2


def test_global_memory_budget_rejects_cross_node_race():
    s,ledger,_=scheduler(capacity=5)
    s.acquire('a',vcpus=2,memory_mib=3,timeout=.05)
    with pytest.raises(AdmissionTimeout):
        s.acquire('b',vcpus=2,memory_mib=3,timeout=.02)
    assert ledger.pending()==3*MIB and len(s.sessions)==1


def test_waiting_placement_read_cost_is_bounded_by_sampling_epoch(monkeypatch):
    from clawbox.experiments import numa_placement
    now=[100.]
    monkeypatch.setattr(numa_placement.time,'monotonic',lambda:now[0])
    s,ledger,_=scheduler()
    reads=[0]
    def charge():
        reads[0]+=1
        return 0,0
    with ledger.condition:
        for i in range(60):
            ledger.add((str(i),'create'),scope='n0',amount=MIB,local_amount=MIB,sample=charge)
        reads[0]=0
        s._placement_memory('n0',1)
        first=reads[0]
        assert first>0
        for _ in range(300):
            s._placement_memory('n0',1)
        assert reads[0]==first
        now[0]+=.21
        s._placement_memory('n0',1)
        assert reads[0]>first


def test_stale_placement_ranking_never_bypasses_live_memory_admission():
    used=[0,0]
    s,ledger,_=scheduler(used=used)
    with ledger.condition:
        for key in s.domains:
            s._placement_memory(key,1)
    used[:]=[10,10]
    with pytest.raises(AdmissionTimeout):
        s.acquire('new',vcpus=1,memory_mib=1,timeout=.01)
    assert not ledger.claims and not s.sessions


def test_placement_event_failure_releases_claim_and_cpu_slot():
    s,ledger,_=scheduler()
    for domain in s.domains.values():
        domain.coordinator.resident_capacity_accounting = True
    def fail(_):raise OSError('event sink failed')
    s.emit=fail
    with pytest.raises(OSError,match='event sink'):
        s.acquire('a',vcpus=2,memory_mib=3,timeout=.1)
    assert not ledger.claims and not s.sessions


def test_impossible_cpu_request_fails_actionably():
    s,ledger,_=scheduler()
    with pytest.raises(ValueError,match='placement_cpu_overcommit'):
        s.acquire('a',vcpus=3,memory_mib=1,timeout=.1)
    assert not ledger.claims


def test_impossible_memory_request_fails_without_waiting_for_timeout():
    s,ledger,_=scheduler()
    with pytest.raises(ValueError, match='compute-node HIGH watermark'):
        s.acquire('a',vcpus=2,memory_mib=11,timeout=.1)
    assert not ledger.claims


def test_spec_rejects_vm_pair_that_cannot_fit_compute_high_watermark():
    constrained = tuple(node.model_copy(update={
        'low_watermark_mib': 2048,
        'high_watermark_mib': 3072,
    }) for node in nodes())
    with pytest.raises(ValueError, match='VM pair plus policy operation headroom'):
        configure_experiment(
            Path('examples/experiments/getting-started.yaml'),
            baseline_names=['uncontrolled-resident'],
            compute_nodes=[node.model_dump() for node in constrained],
            pool_memory_gib=6,
        )


def test_cpu_monitor_measures_deltas_and_reports_missing_pressure(tmp_path):
    (tmp_path/'stat').write_text('cpu0 10 0 0 90 0 0 0 0 0 0\n')
    m=CpuMonitor({'n':'0'},proc=tmp_path,interval=0)
    assert m.values['n']['cpu_utilization'] is None
    (tmp_path/'stat').write_text('cpu0 40 0 0 160 0 0 0 0 0 0\n')
    measured=m.sample()['n']
    assert measured['cpu_utilization']==pytest.approx(.3)
    assert measured['scheduling_pressure'] is None
    with pytest.raises(RuntimeError,match='missing'):
        CpuMonitor({'n':'1'},proc=tmp_path)


def test_os_default_is_a_real_placement_policy_not_pool_toggle():
    spec=configure_experiment(Path('examples/experiments/getting-started.yaml'),
        compute_nodes=[n.model_dump() for n in nodes()],placement_policy='os_default',
        pool_memory_gib=64)
    assert spec.execution.placement_policy=='os_default'
    assert spec.resources.compute_nodes
    raw=spec.model_dump(mode='json')
    raw['resources'].update(warm_numa_node=2,warm_memory_capacity_mib=131072,
                            shared_memory_borrow_limit_mib=65536)
    with pytest.raises(ValueError,match='unbound allocation'):
        ExperimentSpec.model_validate(raw)


@pytest.mark.parametrize('value',[0,float('inf'),float('nan')])
def test_cpu_overcommit_must_be_finite_and_at_least_one(value):
    with pytest.raises(ValueError):ExecutionSpec(placement_cpu_overcommit=value)


def test_resident_capacity_placement_ignores_physical_pressure_and_watermark():
    s, ledger, events = scheduler(used=(100, 100), capacity=1, overcommit=100)
    for domain in s.domains.values():
        domain.coordinator.resident_capacity_accounting = True
        domain.coordinator.set_new_session_admission_blocked(True)
    assert s.acquire('a', vcpus=2, memory_mib=10, timeout=.1, fixed='n0') == 'n0'
    c = s.domains['n0'].coordinator
    c.release('a', 10, wait_class='create')
    with pytest.raises(AdmissionTimeout):
        s.acquire('b', vcpus=2, memory_mib=1, timeout=.01, fixed='n0')
    c.unregister('a')
    s.release('a')
    assert s.acquire('b', vcpus=2, memory_mib=10, timeout=.5, fixed='n0') == 'n0'
