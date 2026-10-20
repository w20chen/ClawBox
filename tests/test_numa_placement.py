from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

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


def test_reporting_kernel_reads_do_not_hold_admission_lock():
    from threading import Event
    s,ledger,_=scheduler()
    reading,finish=Event(),Event()
    def slow_charge():
        reading.set()
        assert finish.wait(2)
        return 0,0
    with ledger.condition:
        ledger.add(('old','create'),scope='n0',amount=MIB,local_amount=MIB)
        ledger.claims['old','create'].sample=slow_charge
    with ThreadPoolExecutor(2) as pool:
        report=pool.submit(s.sample)
        assert reading.wait(1)
        try:
            def take_lock():
                with ledger.condition:
                    return True
            assert pool.submit(take_lock).result(timeout=.5)
        finally:
            finish.set()
        assert report.result()['n0']['outstanding_bytes']==MIB


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
