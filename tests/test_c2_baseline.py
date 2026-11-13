"""Behavioral regressions from the frozen original C2 baseline."""
from pathlib import Path
import pytest
from clawbox.experiments.priority import PriorityDomain


def test_c2_poll_hands_off_to_completion_and_skips_its_stale_pending_entry(tmp_path):
    from threading import Event, Thread
    import time
    domain = PriorityDomain(tmp_path, concurrency=2, repetition=0, legacy=False,
                            topology_mode='c2', topology_min_duration_seconds=.01,
                            placements={0: ('0-15', 0), 1: ('0-15', 0)})
    domain.acquire_tool_call(0, 'first', vcpus=2)
    domain.acquire_tool_call(1, 'finished', vcpus=2)
    domain._pending_started = {key: stamp - 1 for key, stamp in domain._pending_started.items()}
    entered, resume = Event(), Event()
    assigned, errors = [], []

    def assign(index, execution, **kwargs):
        assigned.append(index)
        entered.set()
        assert resume.wait(2)
        return {'release_required': True}

    domain._assign_tool_call = assign
    def run(function):
        try:
            function()
        except BaseException as exc:
            errors.append(exc)
    poll = Thread(target=run, args=(domain.sample_topology_placement,), daemon=True)
    completion = Thread(target=run, args=(lambda: domain.release_tool_call('finished'),), daemon=True)
    poll.start()
    try:
        assert entered.wait(2)
        completion.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with domain._topology_lock._guard:
                if domain._topology_lock._progress:
                    break
            time.sleep(.001)
        else:
            raise AssertionError('completion did not enqueue')
    finally:
        resume.set()
        poll.join(2)
        if completion.ident is not None:
            completion.join(2)
    assert not errors
    assert not poll.is_alive() and not completion.is_alive()
    assert assigned == [0]
    assert domain.active_tool_call_ids() == ('first',)


def c2_domain(monkeypatch, tmp_path):
    monkeypatch.setattr(
        'clawbox.experiments.priority.physical_cluster_groups',
        lambda cpus, min_physical_cores: ('0,2,4,6', '8,10,12,14'),
    )
    events = []
    domain = PriorityDomain(
        tmp_path, concurrency=1, repetition=0, legacy=False,
        topology_mode='c2', topology_min_duration_seconds=0, topology_include_smt=False,
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
        lambda groups, **kwargs: {'0,2,4,6': .8, '8,10,12,14': .1},
    )
    monkeypatch.setattr(domain, '_cpu_observation', lambda: ({}, {}, 0.))
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
        lambda groups, **kwargs: {'0,2,4,6': .1, '8,10,12,14': .8},
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
    clock = [10.0]
    monkeypatch.setattr('clawbox.experiments.priority.time.monotonic', lambda: clock[0])

    deferred = domain.acquire_tool_call(0, 'short', vcpus=2)
    assert deferred['action'] == 'defer'
    clock[0] = 10.5
    assert domain.sample_topology_placement() == []
    completed = domain.release_tool_call('short')
    assert completed['reason'] == 'short_call'
    assert applied == []
    assert domain.topology_summary()['short_call_bypasses'] == 1

    clock[0] = 20.0
    deferred = domain.acquire_tool_call(0, 'long', vcpus=2)
    assert deferred['action'] == 'defer'
    clock[0] = 21.1
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
    clock = [1.0]
    monkeypatch.setattr('clawbox.experiments.priority.time.monotonic', lambda: clock[0])
    domain.acquire_tool_call(0, 'exec-1', vcpus=2)
    clock[0] = 1.25
    (leaf/'cpu.pressure').write_text('some avg10=0 avg60=0 avg300=0 total=200000')
    assert domain.sample_topology_placement() == []
    clock[0] = 1.5
    (leaf/'cpu.pressure').write_text('some avg10=0 avg60=0 avg300=0 total=400000')
    changes = domain.sample_topology_placement()
    assert changes[0]['reason'] == 'cpu_pressure'
    assert changes[0]['to_cpus'] == '0-15'
    assert applied[-1] == (0, 'tool', '0-15')
    assert domain.topology_summary()['total_expansions'] == 1
    assert domain.topology_summary()['active_expanded'] == 1
    assert domain._group_load['8,10,12,14'] == 1
    domain.release_tool_call('exec-1')
    assert domain._group_load['8,10,12,14'] == 0
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
    assert domain._group_load['8,10,12,14'] == 1
    domain.release_tool_call('exec-1')
    assert domain._group_load['8,10,12,14'] == 0
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
    assert sum(domain._group_load.values()) == 0


def test_c2_ignores_prediction_and_keeps_original_cpu_accounting(monkeypatch, tmp_path):
    from clawbox.experiments.llc_c2 import cpu_times
    path = tmp_path/'stat'
    path.write_text('cpu0 60 0 0 40 0 0 0 0 60 0\n')
    assert cpu_times(path)[0] == (120, 160)
    domain, _, _, applied = c2_domain(monkeypatch, tmp_path)
    domain.topology_min_duration_seconds = 1
    event = domain.acquire_tool_call(0, 'call', vcpus=2, prediction={'malformed': True})
    assert event['action'] == 'defer'
    assert not applied
    domain.release_tool_call('call')
    assert domain.topology_summary()['trigger'] == 'elapsed_time'
