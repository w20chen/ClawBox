from pathlib import Path
import json
import os

import pytest

from clawbox.experiments.priority import (
    PriorityDomain, tool_call_cpu_applies, physical_cluster_groups,
)


def long_prediction(ms=5000):
    return {'call_prediction': {'targets': {'duration_ms': {
        'status': 'available', 'unit': 'ms', 'metric_definition': 'retained_workload_elapsed',
        'p50': ms, 'p90': ms * 2, 'sample_count': 5}}}}


def fake_topology(root: Path) -> Path:
    for cpu in range(16):
        topology = root / f'cpu{cpu}' / 'topology'
        topology.mkdir(parents=True)
        cluster = '0-7' if cpu < 8 else '8-15'
        sibling = f'{cpu//2*2}-{cpu//2*2+1}'
        (topology/'cluster_cpus_list').write_text(cluster)
        (topology/'thread_siblings_list').write_text(sibling)
        cache = root / f'cpu{cpu}' / 'cache' / 'index3'
        cache.mkdir(parents=True)
        (cache/'level').write_text('3')
        (cache/'type').write_text('Unified')
        # Kunpeng reports one outer L3 sharing set per NUMA while
        # cluster_cpus_list identifies each four-physical-core LLC cluster.
        (cache/'shared_cpu_list').write_text('0-15')
    return root


def test_groups_use_physical_cores_and_never_cross_llc(tmp_path):
    assert physical_cluster_groups(
        '0-15', min_physical_cores=2, sysfs=fake_topology(tmp_path)
    ) == ('0,2,4,6', '8,10,12,14')


def test_groups_can_include_permitted_smt_siblings(tmp_path):
    assert physical_cluster_groups(
        '0-15', min_physical_cores=2, include_smt=True,
        sysfs=fake_topology(tmp_path),
    ) == ('0-7', '8-15')


def test_groups_accept_smt_disabled_pool_but_reject_partial_physical_cluster(tmp_path):
    topology = fake_topology(tmp_path)
    assert physical_cluster_groups(
        '0,2,4,6,8,10,12,14', min_physical_cores=2, sysfs=topology,
    ) == ('0,2,4,6', '8,10,12,14')
    with pytest.raises(RuntimeError, match='no complete LLC cluster'):
        physical_cluster_groups(
            '0,2,8,10', min_physical_cores=2, sysfs=topology,
        )


def test_groups_fail_instead_of_guessing_missing_cache_topology(tmp_path):
    topology = tmp_path/'cpu0'/'topology'
    topology.mkdir(parents=True)
    (topology/'cluster_cpus_list').write_text('0')
    (topology/'thread_siblings_list').write_text('0')
    with pytest.raises(RuntimeError, match='no readable LLC sharing metadata'):
        physical_cluster_groups('0', min_physical_cores=1, sysfs=tmp_path)


def test_groups_fail_instead_of_treating_outer_l3_as_missing_cluster(tmp_path):
    topology = tmp_path/'cpu0'/'topology'
    topology.mkdir(parents=True)
    (topology/'thread_siblings_list').write_text('0')
    cache = tmp_path/'cpu0'/'cache'/'index3'
    cache.mkdir(parents=True)
    (cache/'level').write_text('3')
    (cache/'type').write_text('Unified')
    (cache/'shared_cpu_list').write_text('0')
    with pytest.raises(RuntimeError, match='no readable LLC cluster metadata'):
        physical_cluster_groups('0', min_physical_cores=1, sysfs=tmp_path)


def test_c0_does_not_discover_sample_or_change_affinity(monkeypatch, tmp_path):
    monkeypatch.setattr(
        'clawbox.experiments.priority.physical_cluster_groups',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('C0 discovered topology')),
    )
    monkeypatch.setattr(
        'clawbox.experiments.priority._cpu_times',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('C0 sampled CPU load')),
    )
    domain = PriorityDomain(tmp_path, concurrency=1, repetition=0, legacy=False,
                            topology_mode='c0')
    domain.configure_topology_placement(0, '0-15', 0)
    assert domain.sample_topology_placement() == []
    assert domain.topology_summary()['assignments'] == 0
    assert domain.topology_summary()['total_expansions'] == 0
    assert domain.topology_summary()['scope'] == 'agent_tool_exec'


def test_tool_call_cpu_applies_only_to_agent_tool_exec():
    assert tool_call_cpu_applies('agent-tool', 'exec') is True
    assert tool_call_cpu_applies('agent-tool', 'filesystem') is False
    assert tool_call_cpu_applies('backend-maintenance', 'exec') is False


def b_domain(monkeypatch, tmp_path):
    monkeypatch.setattr(
        'clawbox.experiments.priority.physical_cluster_groups',
        lambda cpus, min_physical_cores: ('0,2,4,6', '8,10,12,14'),
    )
    events = []
    domain = PriorityDomain(
        tmp_path, concurrency=1, repetition=0, legacy=False,
        topology_mode='b', topology_min_duration_seconds=0,
        topology_include_smt=False,
        placements={0: ('0-15', 0)}, emit=events.append,
    )
    leaf = tmp_path/'tool'
    leaf.mkdir()
    (leaf/'cgroup.procs').write_text('100')
    (leaf/'cpu.pressure').write_text('some avg10=0 avg60=0 avg300=0 total=0')
    domain.leaves[0, 'tool'] = leaf
    applied = []
    monkeypatch.setattr(
        domain, '_apply_thread_affinity',
        lambda index, role, cpus, strict=True: applied.append((index, role, cpus)) or [
            {'tid': 100, 'name': 'vcpu0', 'cpus_allowed': cpus},
        ],
    )
    monkeypatch.setattr(
        domain, '_admission_group_utilization',
        lambda groups, **kwargs: {'0,2,4,6': .8, '8,10,12,14': .1},
    )
    monkeypatch.setattr(domain, '_cpu_observation', lambda: ({}, {}, 0.))
    return domain, leaf, events, applied


def test_b_binds_only_for_active_tool_call_and_restores_c0(monkeypatch, tmp_path):
    domain, _leaf, events, applied = b_domain(monkeypatch, tmp_path)
    assigned = domain.acquire_tool_call(0, 'exec-1', vcpus=2, prediction=long_prediction())
    assert assigned['selected_cpus'] == '8,10,12,14'
    assert applied == [(0, 'tool', '8,10,12,14')]

    shared = domain.acquire_tool_call(0, 'exec-2', vcpus=2, prediction=long_prediction())
    assert shared['action'] == 'share'
    domain.release_tool_call('exec-1')
    assert applied == [(0, 'tool', '8,10,12,14')]
    restored = domain.release_tool_call('exec-2')
    assert restored['action'] == 'restore_c0'
    assert applied[-1] == (0, 'tool', '0-15')
    assert domain.active_tool_call_ids() == ()
    assert [event['action'] for event in events] == [
        'assign', 'share', 'unshare', 'restore_c0',
    ]


def test_b_caches_topology_and_reuses_preferred_group(monkeypatch, tmp_path):
    domain, _leaf, _events, applied = b_domain(monkeypatch, tmp_path)
    discoveries = []
    monkeypatch.setattr(
        'clawbox.experiments.priority.physical_cluster_groups',
        lambda cpus, min_physical_cores: discoveries.append((cpus, min_physical_cores))
        or ('0,2,4,6', '8,10,12,14'),
    )

    first = domain.acquire_tool_call(0, 'exec-1', vcpus=2, prediction=long_prediction())
    assert first['selected_cpus'] == '8,10,12,14'
    assert first['preferred_group_reused'] is False
    domain.release_tool_call('exec-1')
    monkeypatch.setattr(
        domain, '_admission_group_utilization',
        lambda groups, **kwargs: {'0,2,4,6': .1, '8,10,12,14': .8},
    )

    second = domain.acquire_tool_call(0, 'exec-2', vcpus=2, prediction=long_prediction())
    assert second['selected_cpus'] == '8,10,12,14'
    assert second['preferred_group_reused'] is True
    assert discoveries == [('0-15', 2)]
    assert applied[-1] == (0, 'tool', '8,10,12,14')


@pytest.mark.parametrize('order,expected', [('preferred', '8,10,12,14'),
                                           ('utilization', '0,2,4,6')])
def test_group_order_changes_preference_only_after_bound_count(monkeypatch, tmp_path, order, expected):
    domain, _, _, _ = b_domain(monkeypatch, tmp_path)
    domain.topology_group_order = order
    domain._preferred_groups[0, 'tool'] = '8,10,12,14'
    monkeypatch.setattr(domain, '_admission_group_utilization',
                        lambda groups: {'0,2,4,6': .1, '8,10,12,14': .8})
    event = domain.acquire_tool_call(0, 'one', vcpus=2, prediction=long_prediction())
    assert event['selected_cpus'] == expected
    assert event['group_order'] == order
    domain.release_tool_call('one')
    domain._group_load['0,2,4,6'] = 1
    assert domain.acquire_tool_call(0, 'two', vcpus=2, prediction=long_prediction())['selected_cpus'] == '8,10,12,14'


def test_wait_observation_is_diagnostic_and_binding_scoped(monkeypatch, tmp_path):
    domain, _, events, _ = b_domain(monkeypatch, tmp_path)
    domain.topology_observe_wait = True
    domain._vcpu_threads[0, 'tool'] = ((100, 'vcpu0'),)
    clock, counter = [10.], ['100000000 20000000 1']
    original = Path.read_text
    def read(path, *args, **kwargs):
        if str(path).replace('\\', '/') == '/proc/100/schedstat':
            if counter[0] is None:
                raise FileNotFoundError(path)
            return counter[0]
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr('clawbox.experiments.priority.time.monotonic', lambda: clock[0])
    assigned = domain.acquire_tool_call(0, 'one', vcpus=2, prediction=long_prediction())
    fine = assigned['selected_cpus']
    def sample(value):
        clock[0] += .25
        counter[0] = value
        domain._observe_vcpu_wait((0, 'tool'), fine=fine, phase='sample')
        return events[-1]['threads'][0]
    row = sample('200000000 120000000 2')
    assert row['status'] == 'measured' and row['wait_demand_ratio'] == .5
    assert row['elapsed_seconds'] == .25
    assert sample('200000000 120000000 2')['wait_demand_ratio'] is None
    assert sample('0 0 0')['status'] == 'reset'
    assert sample(None)['status'] == 'unavailable'
    assert sample('300000000 300000000 3')['status'] == 'baseline'
    assert domain.topology_summary()['total_expansions'] == 0
    domain.release_tool_call('one')
    assert not domain._wait_previous
    assert events[-2]['phase'] == 'release'
    domain.acquire_tool_call(0, 'two', vcpus=2, prediction=long_prediction())
    assert events[-2]['threads'][0]['status'] == 'baseline'


def test_b_bypasses_when_every_group_reaches_active_limit(monkeypatch, tmp_path):
    domain, _leaf, events, _applied = b_domain(monkeypatch, tmp_path)
    domain.topology_group_max_active = 1
    for index in (1, 2):
        domain.configure_topology_placement(index, '0-15', 0)
        leaf = tmp_path/f'tool-{index}'
        leaf.mkdir()
        (leaf/'cgroup.procs').write_text('100')
        (leaf/'cpu.pressure').write_text('some avg10=0 avg60=0 avg300=0 total=0')
        domain.leaves[index, 'tool'] = leaf

    assert domain.acquire_tool_call(0, 'exec-0', vcpus=2, prediction=long_prediction())['action'] == 'assign'
    assert domain.acquire_tool_call(1, 'exec-1', vcpus=2, prediction=long_prediction())['action'] == 'assign'
    bypass = domain.acquire_tool_call(2, 'exec-2', vcpus=2, prediction=long_prediction())

    assert bypass['action'] == 'bypass_full_numa'
    assert bypass['release_required'] is True
    assert bypass['group_active_assignments'] == {
        '0,2,4,6': 1, '8,10,12,14': 1,
    }
    assert domain.topology_summary()['capacity_bypasses'] == 1
    assert events[-1] == bypass


def test_prediction_threshold_binds_before_exec_without_polling(monkeypatch, tmp_path):
    domain, _leaf, events, applied = b_domain(monkeypatch, tmp_path)
    domain.topology_min_duration_seconds = 1.0
    for label, prediction, reason in [('unknown', None, 'prediction_unavailable'),
                                     ('short', long_prediction(999), 'predicted_short'),
                                     ('boundary', long_prediction(1000), 'predicted_short')]:
        event = domain.acquire_tool_call(0, label, vcpus=2, prediction=prediction)
        assert event['reason'] == reason
        domain.sample_topology_placement()
        domain.release_tool_call(label)
        assert not applied
    event = domain.acquire_tool_call(0, 'long', vcpus=2, prediction=long_prediction(1001))
    assert event['action'] == 'assign'
    assert event['predicted_duration_seconds'] == 1.001
    assert applied[-1] == (0, 'tool', '8,10,12,14')
    domain.release_tool_call('long')


@pytest.mark.parametrize('eligible', [False, True])
def test_b_timeout_quiesces_before_vm_destruction(monkeypatch, tmp_path, eligible):
    domain, _leaf, events, applied = b_domain(monkeypatch, tmp_path)
    domain.acquire_tool_call(0, 'cancelled', vcpus=2,
                             prediction=long_prediction() if eligible else None)
    domain.begin_teardown(cancel_calls=True)
    before = list(applied)
    monkeypatch.setattr(domain, '_apply_thread_affinity',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('VM destroyed')))
    assert domain.sample_topology_placement() == []
    assert domain.release_tool_call('cancelled') is None
    assert domain.active_tool_call_ids() == ()
    assert applied == before
    assert events[-1]['action'] == 'cancel_for_teardown'
    assert events[-1]['execution_ids'] == ['cancelled']
    with pytest.raises(RuntimeError, match='stopping'):
        domain.acquire_tool_call(0, 'late', vcpus=2, prediction=long_prediction())
    domain.begin_teardown(cancel_calls=True)
    assert sum(e.get('action') == 'cancel_for_teardown' for e in events) == 1


def test_b_normal_teardown_does_not_hide_unreleased_calls(monkeypatch, tmp_path):
    domain, _leaf, _events, _applied = b_domain(monkeypatch, tmp_path)
    domain.acquire_tool_call(0, 'unreleased', vcpus=2, prediction=long_prediction())
    domain.begin_teardown()
    assert domain.sample_topology_placement() == []
    assert domain.active_tool_call_ids() == ('unreleased',)


def test_load_gate_rejects_busy_or_unknown_candidates(monkeypatch, tmp_path):
    domain, _leaf, events, applied = b_domain(monkeypatch, tmp_path)
    domain.topology_max_utilization = .9
    for values, reason in [({'0,2,4,6': .9, '8,10,12,14': 1.}, 'cpu_busy'),
                           ({'0,2,4,6': None, '8,10,12,14': .99}, 'load_unavailable')]:
        monkeypatch.setattr(domain, '_admission_group_utilization', lambda groups: values)
        event = domain.acquire_tool_call(0, reason, vcpus=2, prediction=long_prediction())
        assert event['reason'] == reason
        assert domain.active_tool_call_ids() == (reason,)
        domain.release_tool_call(reason)
    assert not applied
    assert not domain._group_load


def test_long_call_releases_binding_while_short_call_remains(monkeypatch, tmp_path):
    domain, _leaf, events, applied = b_domain(monkeypatch, tmp_path)
    domain.topology_min_duration_seconds = 1.
    domain.acquire_tool_call(0, 'short', vcpus=2, prediction=long_prediction(100))
    assert not applied
    domain.acquire_tool_call(0, 'long', vcpus=2, prediction=long_prediction())
    assert sum(domain._group_load.values()) == 1
    domain.release_tool_call('long')
    assert applied[-1] == (0, 'tool', '0-15')
    assert sum(domain._group_load.values()) == 0
    assert domain.active_tool_call_ids() == ('short',)
    domain.release_tool_call('short')
    assert sum(domain._group_load.values()) == 0


def test_b_expands_active_call_after_pressure_samples(monkeypatch, tmp_path):
    domain, leaf, _events, applied = b_domain(monkeypatch, tmp_path)
    now = [1.]
    monkeypatch.setattr('clawbox.experiments.priority.time.monotonic', lambda: now[0])
    domain.acquire_tool_call(0, 'exec-1', vcpus=2, prediction=long_prediction())
    now[0] = 2.
    (leaf/'cpu.pressure').write_text('some avg10=0 avg60=0 avg300=0 total=200000')
    assert domain.sample_topology_placement() == []
    now[0] = 3.
    (leaf/'cpu.pressure').write_text('some avg10=0 avg60=0 avg300=0 total=400000')
    changes = domain.sample_topology_placement()
    assert changes[0]['reason'] == 'cpu_pressure'
    assert changes[0]['to_cpus'] == '0-15'
    assert applied[-1] == (0, 'tool', '0-15')
    assert domain.topology_summary()['total_expansions'] == 1
    assert domain.topology_summary()['active_expanded'] == 1
    assert sum(domain._group_load.values()) == 0
    assert domain.topology_summary()['active_assignments'] == 0
    assert changes[0]['bound_seconds'] == 2.
    assert domain.acquire_tool_call(0, 'exec-2', vcpus=2, prediction=long_prediction())['reason'] == 'already_expanded'
    domain.release_tool_call('exec-2')
    domain.release_tool_call('exec-1')
    assert sum(domain._group_load.values()) == 0
    assert domain.topology_summary()['active_expanded'] == 0
    assert domain.topology_summary()['total_expansions'] == 1


@pytest.mark.parametrize('intervals', [
    [.01, .25, .25, .25, .25, .25, .25, .25, .25],
    [.125] * 16,
    [.2, .6, .4, .8],
])
def test_b_pressure_confirmation_uses_elapsed_time(monkeypatch, tmp_path, intervals):
    domain, leaf, _events, _applied = b_domain(monkeypatch, tmp_path)
    now = [10.]
    monkeypatch.setattr('clawbox.experiments.priority.time.monotonic', lambda: now[0])
    domain.acquire_tool_call(0, 'exec-1', vcpus=2, prediction=long_prediction())
    total = 0
    for interval in intervals:
        now[0] += interval
        total += round(interval * 500_000)
        (leaf/'cpu.pressure').write_text(f'some total={total}')
        changes = domain.sample_topology_placement()
        if now[0] - 10. < 2.:
            assert changes == []
        else:
            assert changes[0]['pressure_high_seconds'] >= 2.
            assert changes[0]['slot_released'] is True
    assert domain.topology_summary()['total_expansions'] == 1


@pytest.mark.parametrize('reset_counter', [False, True])
def test_b_pressure_confirmation_resets(monkeypatch, tmp_path, reset_counter):
    domain, leaf, _events, _applied = b_domain(monkeypatch, tmp_path)
    now = [10.]
    monkeypatch.setattr('clawbox.experiments.priority.time.monotonic', lambda: now[0])
    domain.acquire_tool_call(0, 'exec-1', vcpus=2, prediction=long_prediction())
    values = [500000, 0 if reset_counter else 500000]
    base = values[-1]
    values.extend([base + 500000, base + 1000000])
    for i, total in enumerate(values):
        now[0] += 1.
        (leaf/'cpu.pressure').write_text(f'some total={total}')
        changes = domain.sample_topology_placement()
        if i < 3:
            assert changes == []
        else:
            assert changes[0]['pressure_high_seconds'] == 2.


def test_b_utilization_veto_can_be_disabled_without_disabling_slots(monkeypatch, tmp_path):
    domain, _leaf, _events, _applied = b_domain(monkeypatch, tmp_path)
    assert domain.topology_max_utilization is None
    domain.topology_max_utilization = .9
    monkeypatch.setattr(domain, '_admission_group_utilization',
                        lambda groups: {g: 1. for g in groups})
    assert domain.acquire_tool_call(0, 'busy', vcpus=2, prediction=long_prediction())['reason'] == 'cpu_busy'
    domain.release_tool_call('busy')
    domain.topology_max_utilization = None
    assigned = domain.acquire_tool_call(0, 'allowed', vcpus=2, prediction=long_prediction())
    assert assigned['action'] == 'assign'
    assert sum(domain._group_load.values()) == 1
    domain.release_tool_call('allowed')
    domain._group_load = {g: 2 for g in domain._groups_by_pool[next(iter(domain._groups_by_pool))]}
    assert domain.acquire_tool_call(0, 'full', vcpus=2, prediction=long_prediction())['reason'] == 'group_capacity'
    domain.release_tool_call('full')


def test_b_counts_pressure_unavailable_expansion(monkeypatch, tmp_path):
    domain, leaf, _events, _applied = b_domain(monkeypatch, tmp_path)
    domain.acquire_tool_call(0, 'exec-1', vcpus=2, prediction=long_prediction())
    (leaf/'cpu.pressure').write_text('invalid')

    changes = domain.sample_topology_placement()

    assert changes[0]['reason'] == 'pressure_unavailable'
    assert domain.topology_summary()['total_expansions'] == 1
    assert domain.topology_summary()['active_expanded'] == 1
    assert sum(domain._group_load.values()) == 0
    assert domain.topology_summary()['active_assignments'] == 0
    assert changes[0]['bound_seconds'] >= 0.
    assert domain.acquire_tool_call(0, 'exec-2', vcpus=2, prediction=long_prediction())['reason'] == 'already_expanded'
    domain.release_tool_call('exec-2')
    domain.release_tool_call('exec-1')
    assert sum(domain._group_load.values()) == 0
    assert domain.topology_summary()['active_expanded'] == 0
    assert domain.topology_summary()['total_expansions'] == 1


def test_b_failed_affinity_restores_c0_without_publishing_assignment(monkeypatch, tmp_path):
    domain, _leaf, events, applied = b_domain(monkeypatch, tmp_path)

    def fail_narrow(index, role, cpus, strict=True):
        applied.append((index, role, cpus, strict))
        if cpus == '8,10,12,14':
            raise RuntimeError('partial affinity failure')
        return [{'tid': 100, 'name': 'vcpu0', 'cpus_allowed': cpus}]

    monkeypatch.setattr(domain, '_apply_thread_affinity', fail_narrow)
    try:
        domain.acquire_tool_call(0, 'exec-1', vcpus=2, prediction=long_prediction())
    except RuntimeError as exc:
        assert str(exc) == 'partial affinity failure'
    else:
        raise AssertionError('C2 affinity failure was not propagated')

    assert applied == [
        (0, 'tool', '8,10,12,14', True),
        (0, 'tool', '0-15', False),
    ]
    assert domain.active_tool_call_ids() == ()
    assert domain.topology_summary()['assignments'] == 0
    assert events == []
    assert sum(domain._group_load.values()) == 0


def test_slow_affinity_is_per_vm_and_reserves_group_capacity(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    domain, leaf, _, _ = b_domain(monkeypatch, tmp_path)
    domain.topology_group_max_active = 1
    for index in (1, 2):
        domain.configure_topology_placement(index, '0-15', 0)
        domain.leaves[index, 'tool'] = leaf
    entered, resume = Event(), Event()

    def affinity(index, role, cpus, *, strict):
        if index == 0 and cpus != '0-15':
            entered.set()
            assert resume.wait(3)
        return [{'tid': 100 + index, 'cpus_allowed': cpus}]

    monkeypatch.setattr(domain, '_apply_thread_affinity',
                        PriorityDomain._apply_thread_affinity.__get__(domain))
    monkeypatch.setattr(domain, '_apply_thread_affinity_io', affinity)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(domain.acquire_tool_call, 0, 'first', vcpus=2, prediction=long_prediction())
        try:
            assert entered.wait(1)
            other = pool.submit(domain.acquire_tool_call, 1, 'other', vcpus=2, prediction=long_prediction()).result(timeout=1)
            assert other['selected_cpus'] == '0,2,4,6'
            bypass = pool.submit(domain.acquire_tool_call, 2, 'full', vcpus=2, prediction=long_prediction()).result(timeout=1)
            assert bypass['action'] == 'bypass_full_numa'
            pool.submit(domain.release_tool_call, 'full').result(timeout=1)
            pool.submit(domain.release_tool_call, 'other').result(timeout=1)
            pool.submit(domain.configure_topology_placement, 3, '0-15', 0).result(timeout=1)
        finally:
            resume.set()
        first.result(timeout=2)
    domain.release_tool_call('first')
    assert sum(domain._group_load.values()) == 0
    assert domain.active_tool_call_ids() == ()


def test_teardown_drains_inflight_affinity_before_clearing_ownership(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    domain, _, _, _ = b_domain(monkeypatch, tmp_path)
    entered, resume, stopping = Event(), Event(), Event()
    def affinity(index, role, cpus, *, strict):
        entered.set()
        assert resume.wait(3)
        return [{'tid': 100, 'cpus_allowed': cpus}]
    monkeypatch.setattr(domain, '_apply_thread_affinity',
                        PriorityDomain._apply_thread_affinity.__get__(domain))
    monkeypatch.setattr(domain, '_apply_thread_affinity_io', affinity)
    def teardown():
        stopping.set()
        domain.begin_teardown(cancel_calls=True)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(domain.acquire_tool_call, 0, 'first', vcpus=2, prediction=long_prediction())
        try:
            assert entered.wait(1)
            cleanup = pool.submit(teardown)
            assert stopping.wait(1)
            assert not cleanup.done()
        finally:
            resume.set()
        first.result(timeout=2)
        cleanup.result(timeout=2)
    assert domain.active_tool_call_ids() == ()
    assert not domain._group_load


@pytest.mark.parametrize("disappeared", [FileNotFoundError, ProcessLookupError])
def test_vm_verification_tolerates_exited_auxiliary_thread(monkeypatch, tmp_path, disappeared):
    from pathlib import Path
    mount = tmp_path/'sys/fs/cgroup'
    parent = mount/'cube_sandbox/sandbox'
    leaf = parent/'42'
    leaf.mkdir(parents=True)
    registry = tmp_path/'registry'
    registry.mkdir()
    domain = PriorityDomain(
        parent, concurrency=1, repetition=0, legacy=False,
        placements={0: ('0-1', 0)}, registry=registry, cgroup_mount=mount,
    )
    domain.leaves[0, 'tool'] = leaf
    for name, value in {'cpuset.cpus.effective': '0-1', 'cpuset.mems.effective': '0',
                        'cgroup.threads': '100 101'}.items():
        (leaf / name).write_text(value)
    (registry/domain.token(0, 'tool')).write_text(json.dumps({
        'numa_node': 0, 'path': '/cube_sandbox/sandbox/42',
    }))
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path.as_posix() == '/proc/100/status':
            raise disappeared(3, 'No such process')
        if path.as_posix() == '/proc/101/status':
            return 'Name: vcpu0\nCpus_allowed_list: 0-1\n'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    applied = []
    monkeypatch.setattr(os, 'sched_setaffinity',
                        lambda tid, cpus: applied.append((tid, set(cpus))),
                        raising=False)
    monkeypatch.setattr(os, 'sched_getaffinity', lambda tid: {0, 1},
                        raising=False)
    monkeypatch.setattr('clawbox.experiments.numa_borrow.read_numa_lru_bytes',
                        lambda path: ({0: 1024}, 0))
    monkeypatch.setattr('clawbox.experiments.numa_borrow.read_numa_anon_bytes',
                        lambda path: {0: 1024})
    assert [t['tid'] for t in domain.verify(0, 'tool', 'vm')['threads']] == [101]
    assert applied == [(101, {0, 1})]
    # A freshly restored incremental snapshot can be entirely file-backed.
    # Shared file cache is not evidence about the VMM allocation policy.
    monkeypatch.setattr('clawbox.experiments.numa_borrow.read_numa_lru_bytes',
                        lambda path: ({0: 1, 1: 1024}, 0))
    monkeypatch.setattr('clawbox.experiments.numa_borrow.read_numa_anon_bytes',
                        lambda path: {0: 0, 1: 0})
    restored = domain.verify(0, 'tool', 'vm')
    assert restored['residency_basis'] == 'restore_policy_only'
    assert restored['local_residency_fraction'] is None
    monkeypatch.setattr('clawbox.experiments.numa_borrow.read_numa_anon_bytes',
                        lambda path: {0: 1024, 1: 16 * 1024 * 1024})
    domain._initial_residency_verified.clear()
    with pytest.raises(RuntimeError, match='memory placement is not local'):
        domain.verify(0, 'tool', 'vm')
    # An empty vCPU set must still fail: disappearance is not evidence of routing.
    (leaf / 'cgroup.threads').write_text('100')
    domain.native_vcpu_ready_grace_seconds = 0
    with pytest.raises(RuntimeError, match='no vCPU threads'):
        domain.verify(0, 'tool', 'vm')


def test_native_vm_verification_waits_for_vcpu_threads(monkeypatch, tmp_path):
    mount = tmp_path/'sys/fs/cgroup'
    parent = mount/'cube_sandbox/sandbox'
    leaf = parent/'42'
    leaf.mkdir(parents=True)
    registry = tmp_path/'registry'
    registry.mkdir()
    domain = PriorityDomain(
        parent, concurrency=1, repetition=0, legacy=False,
        placements={0: ('0-1', 0)}, registry=registry, cgroup_mount=mount,
    )
    domain.native_vcpu_ready_grace_seconds = 1
    for name, value in {
        'cpuset.cpus.effective': '0-1',
        'cpuset.mems.effective': '0',
        'cgroup.threads': '',
    }.items():
        (leaf / name).write_text(value)
    (registry/domain.token(0, 'tool')).write_text(json.dumps({
        'numa_node': 0, 'path': '/cube_sandbox/sandbox/42',
    }))
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path.as_posix() == '/proc/101/status':
            return 'Name: vcpu0\nCpus_allowed_list: 0-1\n'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(os, 'sched_setaffinity', lambda tid, cpus: None,
                        raising=False)
    monkeypatch.setattr(os, 'sched_getaffinity', lambda tid: {0, 1},
                        raising=False)
    def expose_vcpu(_delay):
        (leaf / 'cgroup.threads').write_text('101')
    monkeypatch.setattr('clawbox.experiments.priority.time.sleep', expose_vcpu)
    monkeypatch.setattr('clawbox.experiments.numa_borrow.read_numa_lru_bytes',
                        lambda path: ({0: 1024}, 0))
    monkeypatch.setattr('clawbox.experiments.numa_borrow.read_numa_anon_bytes',
                        lambda path: {0: 1024})

    result = domain.verify(0, 'tool', 'vm')

    assert [thread['tid'] for thread in result['threads']] == [101]


def test_cpu_accounting_does_not_double_count_guest(tmp_path):
    from clawbox.experiments.priority import _cpu_times
    path = tmp_path/'stat'
    path.write_text('cpu0 60 0 0 40 0 0 0 0 60 0\n')
    assert _cpu_times(path)[0] == (60, 100)


def test_admission_uses_shared_sample_only_while_fresh(tmp_path, monkeypatch):
    domain = PriorityDomain(tmp_path, concurrency=1, repetition=0, legacy=False,
                            topology_mode='b')
    domain._latest_group_utilization = {'0-7': .4}
    domain._cpu_sample_at = 10.
    monkeypatch.setattr('clawbox.experiments.priority.time.monotonic', lambda: 10.1)
    assert domain._admission_group_utilization(['0-7', '8-15']) == {'0-7': .4, '8-15': None}
    monkeypatch.setattr('clawbox.experiments.priority.time.monotonic', lambda: 10.6)
    assert domain._admission_group_utilization(['0-7']) == {'0-7': None}


@pytest.mark.parametrize('value', [True, float('nan'), -1])
def test_invalid_available_duration_is_an_error(value):
    from clawbox.experiments.priority import tool_duration_prediction
    with pytest.raises(ValueError, match='duration_ms'):
        tool_duration_prediction(long_prediction(value))


def test_duration_uses_canonical_tool_prediction_not_memory_fallback():
    from clawbox.experiments.priority import tool_duration_prediction
    prediction = long_prediction(9000)
    prediction['models'] = {'tool': long_prediction(500)['call_prediction']}
    assert tool_duration_prediction(prediction)['predicted_duration_seconds'] == .5
