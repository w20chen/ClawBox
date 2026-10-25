from pathlib import Path
import json

import pytest

from clawbox.experiments.priority import (
    PriorityDomain, c2_applies, physical_cluster_groups,
)


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


def test_c2_applies_only_to_agent_tool_exec():
    assert c2_applies('agent-tool', 'exec') is True
    assert c2_applies('agent-tool', 'filesystem') is False
    assert c2_applies('backend-maintenance', 'exec') is False


def c2_domain(monkeypatch, tmp_path):
    monkeypatch.setattr(
        'clawbox.experiments.priority.physical_cluster_groups',
        lambda cpus, min_physical_cores: ('0,2,4,6', '8,10,12,14'),
    )
    events = []
    domain = PriorityDomain(
        tmp_path, concurrency=1, repetition=0, legacy=False,
        topology_mode='c2', topology_min_duration_seconds=0,
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
        domain, '_sample_group_utilization',
        lambda groups: {'0,2,4,6': .8, '8,10,12,14': .1},
    )
    return domain, leaf, events, applied


def test_c2_binds_only_for_active_tool_call_and_restores_c0(monkeypatch, tmp_path):
    domain, _leaf, events, applied = c2_domain(monkeypatch, tmp_path)
    assigned = domain.acquire_tool_call(0, 'exec-1', vcpus=2)
    assert assigned['selected_cpus'] == '8,10,12,14'
    assert applied == [(0, 'tool', '8,10,12,14')]

    shared = domain.acquire_tool_call(0, 'exec-2', vcpus=2)
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


def test_c2_caches_topology_and_reuses_preferred_group(monkeypatch, tmp_path):
    domain, _leaf, _events, applied = c2_domain(monkeypatch, tmp_path)
    discoveries = []
    monkeypatch.setattr(
        'clawbox.experiments.priority.physical_cluster_groups',
        lambda cpus, min_physical_cores: discoveries.append((cpus, min_physical_cores))
        or ('0,2,4,6', '8,10,12,14'),
    )

    first = domain.acquire_tool_call(0, 'exec-1', vcpus=2)
    assert first['selected_cpus'] == '8,10,12,14'
    assert first['preferred_group_reused'] is False
    domain.release_tool_call('exec-1')
    monkeypatch.setattr(
        domain, '_sample_group_utilization',
        lambda groups: {'0,2,4,6': .1, '8,10,12,14': .8},
    )

    second = domain.acquire_tool_call(0, 'exec-2', vcpus=2)
    assert second['selected_cpus'] == '8,10,12,14'
    assert second['preferred_group_reused'] is True
    assert discoveries == [('0-15', 2)]
    assert applied[-1] == (0, 'tool', '8,10,12,14')


def test_c2_bypasses_when_every_group_reaches_active_limit(monkeypatch, tmp_path):
    domain, _leaf, events, _applied = c2_domain(monkeypatch, tmp_path)
    domain.topology_group_max_active = 1
    for index in (1, 2):
        domain.configure_topology_placement(index, '0-15', 0)
        leaf = tmp_path/f'tool-{index}'
        leaf.mkdir()
        (leaf/'cgroup.procs').write_text('100')
        (leaf/'cpu.pressure').write_text('some avg10=0 avg60=0 avg300=0 total=0')
        domain.leaves[index, 'tool'] = leaf

    assert domain.acquire_tool_call(0, 'exec-0', vcpus=2)['action'] == 'assign'
    assert domain.acquire_tool_call(1, 'exec-1', vcpus=2)['action'] == 'assign'
    bypass = domain.acquire_tool_call(2, 'exec-2', vcpus=2)

    assert bypass['action'] == 'bypass_full_numa'
    assert bypass['release_required'] is False
    assert bypass['group_active_assignments'] == {
        '0,2,4,6': 1, '8,10,12,14': 1,
    }
    assert domain.topology_summary()['capacity_bypasses'] == 1
    assert events[-1] == bypass


def test_c2_delays_affinity_and_skips_short_calls(monkeypatch, tmp_path):
    domain, _leaf, events, applied = c2_domain(monkeypatch, tmp_path)
    domain.topology_min_duration_seconds = 1.0
    times = iter((10.0, 10.5, 20.0, 21.1, 21.1))
    monkeypatch.setattr('clawbox.experiments.priority.time.monotonic', lambda: next(times))

    deferred = domain.acquire_tool_call(0, 'short', vcpus=2)
    assert deferred['action'] == 'defer'
    assert domain.sample_topology_placement() == []
    completed = domain.release_tool_call('short')
    assert completed['reason'] == 'short_call'
    assert applied == []
    assert domain.topology_summary()['short_call_bypasses'] == 1

    deferred = domain.acquire_tool_call(0, 'long', vcpus=2)
    assert deferred['action'] == 'defer'
    changes = domain.sample_topology_placement()
    assert changes[0]['action'] == 'assign'
    assert applied[-1] == (0, 'tool', '8,10,12,14')
    domain.release_tool_call('long')
    assert [event['action'] for event in events if 'action' in event] == [
        'defer', 'complete_full_numa', 'defer', 'assign', 'restore_c0',
    ]


@pytest.mark.parametrize('pending', [False, True])
def test_c2_timeout_quiesces_before_vm_destruction(monkeypatch, tmp_path, pending):
    domain, _leaf, events, applied = c2_domain(monkeypatch, tmp_path)
    domain.topology_min_duration_seconds = 1.0 if pending else 0.0
    domain.acquire_tool_call(0, 'cancelled', vcpus=2)
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
        domain.acquire_tool_call(0, 'late', vcpus=2)
    domain.begin_teardown(cancel_calls=True)
    assert sum(e.get('action') == 'cancel_for_teardown' for e in events) == 1


def test_c2_normal_teardown_does_not_hide_unreleased_calls(monkeypatch, tmp_path):
    domain, _leaf, _events, _applied = c2_domain(monkeypatch, tmp_path)
    domain.acquire_tool_call(0, 'unreleased', vcpus=2)
    domain.begin_teardown()
    assert domain.sample_topology_placement() == []
    assert domain.active_tool_call_ids() == ('unreleased',)


def test_c2_delayed_call_stays_tracked_after_capacity_bypass(monkeypatch, tmp_path):
    domain, _leaf, _events, _applied = c2_domain(monkeypatch, tmp_path)
    domain.topology_min_duration_seconds = 1.0
    domain.topology_group_max_active = 1
    for index in (1, 2):
        domain.configure_topology_placement(index, '0-15', 0)
        leaf = tmp_path/f'delayed-tool-{index}'
        leaf.mkdir()
        (leaf/'cgroup.procs').write_text('100')
        (leaf/'cpu.pressure').write_text('some avg10=0 avg60=0 avg300=0 total=0')
        domain.leaves[index, 'tool'] = leaf
    now = [0.0]
    monkeypatch.setattr('clawbox.experiments.priority.time.monotonic', lambda: now[0])
    for index in range(3):
        assert domain.acquire_tool_call(index, f'exec-{index}', vcpus=2)['action'] == 'defer'

    now[0] = 2.0
    changes = domain.sample_topology_placement()

    assert [event['action'] for event in changes] == [
        'assign', 'assign', 'bypass_full_numa',
    ]
    assert set(domain.active_tool_call_ids()) == {'exec-0', 'exec-1', 'exec-2'}
    completed = domain.release_tool_call('exec-2')
    assert completed['reason'] == 'group_capacity'
    assert completed['capacity_bypassed'] is True
    assert set(domain.active_tool_call_ids()) == {'exec-0', 'exec-1'}


def test_c2_expands_active_call_after_pressure_samples(monkeypatch, tmp_path):
    domain, leaf, _events, applied = c2_domain(monkeypatch, tmp_path)
    times = iter((1.0, 2.0, 3.0, 4.0))
    monkeypatch.setattr('clawbox.experiments.priority.time.monotonic', lambda: next(times))
    domain.acquire_tool_call(0, 'exec-1', vcpus=2)
    (leaf/'cpu.pressure').write_text('some avg10=0 avg60=0 avg300=0 total=200000')
    assert domain.sample_topology_placement() == []
    (leaf/'cpu.pressure').write_text('some avg10=0 avg60=0 avg300=0 total=400000')
    changes = domain.sample_topology_placement()
    assert changes[0]['reason'] == 'cpu_pressure'
    assert changes[0]['to_cpus'] == '0-15'
    assert applied[-1] == (0, 'tool', '0-15')
    assert domain.topology_summary()['total_expansions'] == 1
    assert domain.topology_summary()['active_expanded'] == 1
    domain.release_tool_call('exec-1')
    assert domain.topology_summary()['active_expanded'] == 0
    assert domain.topology_summary()['total_expansions'] == 1


def test_c2_counts_pressure_unavailable_expansion(monkeypatch, tmp_path):
    domain, leaf, _events, _applied = c2_domain(monkeypatch, tmp_path)
    domain.acquire_tool_call(0, 'exec-1', vcpus=2)
    (leaf/'cpu.pressure').write_text('invalid')

    changes = domain.sample_topology_placement()

    assert changes[0]['reason'] == 'pressure_unavailable'
    assert domain.topology_summary()['total_expansions'] == 1
    assert domain.topology_summary()['active_expanded'] == 1
    domain.release_tool_call('exec-1')
    assert domain.topology_summary()['active_expanded'] == 0
    assert domain.topology_summary()['total_expansions'] == 1


def test_c2_failed_affinity_restores_c0_without_publishing_assignment(monkeypatch, tmp_path):
    domain, _leaf, events, applied = c2_domain(monkeypatch, tmp_path)

    def fail_narrow(index, role, cpus, strict=True):
        applied.append((index, role, cpus, strict))
        if cpus == '8,10,12,14':
            raise RuntimeError('partial affinity failure')
        return [{'tid': 100, 'name': 'vcpu0', 'cpus_allowed': cpus}]

    monkeypatch.setattr(domain, '_apply_thread_affinity', fail_narrow)
    try:
        domain.acquire_tool_call(0, 'exec-1', vcpus=2)
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
    monkeypatch.setattr('clawbox.experiments.numa_borrow.read_numa_lru_bytes',
                        lambda path: ({0: 1024}, 0))
    monkeypatch.setattr('clawbox.experiments.numa_borrow.read_numa_anon_bytes',
                        lambda path: {0: 1024})
    assert [t['tid'] for t in domain.verify(0, 'tool', 'vm')['threads']] == [101]
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
    with pytest.raises(RuntimeError, match='no vCPU threads'):
        domain.verify(0, 'tool', 'vm')
