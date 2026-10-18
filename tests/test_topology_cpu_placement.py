from pathlib import Path

import pytest

from clawbox.experiments.priority import PriorityDomain, physical_cluster_groups


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


def c2_domain(monkeypatch, tmp_path):
    monkeypatch.setattr(
        'clawbox.experiments.priority.physical_cluster_groups',
        lambda cpus, min_physical_cores: ('0,2,4,6', '8,10,12,14'),
    )
    events = []
    domain = PriorityDomain(
        tmp_path, concurrency=1, repetition=0, legacy=False,
        topology_mode='c2', placements={0: ('0-15', 0)}, emit=events.append,
        topology_cooldown_seconds=1,
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
