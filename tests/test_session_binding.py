from types import SimpleNamespace
from threading import Event, Thread

import pytest

from clawbox.experiments.session_binding import SessionBinding, choose_resume
from clawbox.experiments.policy import AdmissionRequest
from test_numa_placement import scheduler, MIB


def pair():
    s, ledger, events = scheduler()
    vms = {role: SimpleNamespace(_state='swapped', resident=False) for role in ('runtime', 'tool')}
    source = s.domains['n0'].coordinator
    source.register('session', vms['tool'])
    source.register_runtime('session', vms['runtime'])
    source.begin_model_wait('session', 'request')
    s.sessions['session'] = ('n0', 2, 'template')
    binding = SessionBinding('session', 0, 'n0', s.domains, lifecycles=vms)
    return s, ledger, events, binding, source


def test_cpu_lease_released_only_after_complete_pair_and_reacquired_once():
    s, ledger, _, binding, _ = pair()
    vms = binding.lifecycles
    vms['runtime']._state = 'running'
    assert not s.suspend_pair('session', tool=vms['tool'], runtime=vms['runtime'])
    assert 'session' in s.sessions
    vms['runtime']._state = 'swapped'
    assert s.suspend_pair('session', tool=vms['tool'], runtime=vms['runtime'])
    assert s.suspend_pair('session', tool=vms['tool'], runtime=vms['runtime'])
    assert not s.sessions
    s.resume_cpu('session', 'n1', timeout=.1)
    s.resume_cpu('session', 'n1', timeout=.1)
    assert s.sessions == {'session': ('n1', 2, 'template')}
    assert not ledger.claims


def test_native_success_handoff_preserves_wait_and_unrelated_claims():
    s, ledger, _, binding, source = pair()
    state = source._sessions['session']
    state.response_ready = True
    state.eviction_eligible = False
    with ledger.condition:
        ledger.add(('session', 'lifetime'), scope='n0', amount=2*MIB, local_amount=MIB)
        ledger.add(('session', 'shared-reservation'), scope='shared', amount=MIB, local_amount=MIB)
    retained = ledger.claims['session', 'lifetime']
    epoch, tx = binding.begin('n1')
    binding.mark_restoring(epoch, tx)
    binding.lifecycles['runtime'].resident = True
    binding.lifecycles['runtime']._state = 'running'
    binding.commit(epoch, tx)
    assert 'session' not in source._sessions
    target = s.domains['n1'].coordinator
    assert target._sessions['session'] is state
    assert state.wait_id == 'request' and state.response_ready
    assert not state.migration_in_progress
    assert ledger.claims['session', 'lifetime'] is retained
    assert retained.scope == 'n1'
    assert ledger.claims['session', 'shared-reservation'].scope == 'shared'
    assert ledger.pending_upper_bound() == 3*MIB
    assert ledger.pending_upper_bound('n0') == 0
    assert ledger.pending_upper_bound('n1') == MIB
    assert binding.current == 'n1' and binding.epoch == 1
    with pytest.raises(RuntimeError, match='stale'):
        binding.commit(epoch, tx)


@pytest.mark.parametrize('conflict', ['tool', 'claim', 'waiter', 'eviction', 'unknown'])
def test_migration_rejects_concurrent_or_uncertain_work(conflict):
    s, ledger, _, binding, source = pair()
    if conflict == 'tool':
        source._sessions['session'].tool_active = True
    elif conflict == 'claim':
        with ledger.condition:
            ledger.add(('session', 'tool_admission'), scope='n0', amount=MIB, local_amount=MIB)
    elif conflict == 'waiter':
        source._waiter_requests[object()] = AdmissionRequest('session', MIB, 'tool_admission', False)
    elif conflict == 'eviction':
        source._sessions['session'].eviction_in_progress = True
    else:
        binding.lifecycles['runtime']._state = 'unknown'
    with pytest.raises(RuntimeError):
        binding.begin('n1')
    assert binding.current == 'n0'


def test_transfer_failure_can_return_home_but_unknown_native_cannot():
    _, _, _, binding, source = pair()
    epoch, tx = binding.begin('n1')
    binding.fail(epoch, tx, known_paused=True)
    assert binding.current == 'n0' and binding.transition == 'stable'
    epoch, tx = binding.begin('n1')
    binding.mark_restoring(epoch, tx)
    binding.fail(epoch, tx, known_paused=False)
    assert binding.transition == 'unknown'
    assert source._sessions['session'].migration_in_progress
    with pytest.raises(RuntimeError, match='unknown'):
        binding.begin('n0')


def test_common_selector_counts_bytes_once_and_respects_hysteresis():
    candidates = {
        'n0': dict(eligible=True, resource_wait_seconds=4., artifact_seconds=0., native_restore_seconds=.2),
        'n1': dict(eligible=True, resource_wait_seconds=0., artifact_seconds=5., native_restore_seconds=.2),
    }
    assert choose_resume('n0', candidates, policy='home', threshold_seconds=.25)[0] == 'n0'
    assert choose_resume('n0', candidates, policy='load-aware', threshold_seconds=.25)[0] == 'n1'
    assert choose_resume('n0', candidates, policy='cost-aware', threshold_seconds=.25)[0] == 'n0'
    candidates['n1']['artifact_seconds'] = 1.
    assert choose_resume('n0', candidates, policy='cost-aware', threshold_seconds=.25)[0] == 'n1'
    candidates['n0']['resource_wait_seconds'] = None
    assert choose_resume('n0', candidates, policy='cost-aware', threshold_seconds=.25)[0] == 'n0'


def test_concurrent_restores_cannot_overbook_target_cpu():
    s, _, _, binding, _ = pair()
    s.suspend_pair('session', tool=binding.lifecycles['tool'], runtime=binding.lifecycles['runtime'])
    s.suspended['second'] = ('n0', 2, 'template')
    s.resume_cpu('session', 'n1', timeout=.2)
    started, acquired = Event(), Event()
    errors = []
    def restore_second():
        started.set()
        try:
            s.resume_cpu('second', 'n1', timeout=2)
            acquired.set()
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=restore_second, daemon=True)
    thread.start()
    assert started.wait(1)
    assert not acquired.wait(.05)
    assert sum(item[1] for item in s.sessions.values() if item[0] == 'n1') == 2
    s.release('session')
    thread.join(1)
    assert acquired.is_set() and not errors
    assert s.sessions['second'][0] == 'n1'


def test_priority_handoff_rejects_resident_and_active_roles(tmp_path):
    from clawbox.experiments.priority import PriorityDomain
    domain = PriorityDomain(tmp_path, concurrency=1, repetition=0, legacy=False,
                            placements={0: ('0-1', 0)})
    tool = SimpleNamespace(_state='swapped')
    runtime = SimpleNamespace(_state='running')
    with pytest.raises(RuntimeError, match='SWAPPED'):
        domain.handoff_swapped_placement(0, '2-3', 1, tool=tool, runtime=runtime)
    runtime._state = 'swapped'
    domain._active_calls[0, 'tool'] = {'execution'}
    with pytest.raises(RuntimeError, match='active'):
        domain.handoff_swapped_placement(0, '2-3', 1, tool=tool, runtime=runtime)
    domain._active_calls.clear()
    domain.handoff_swapped_placement(0, '2-3', 1, tool=tool, runtime=runtime)
    assert domain.placements[0] == ('2-3', 1)
    with pytest.raises(RuntimeError, match='changed'):
        domain.configure_topology_placement(0, '0-1', 0)


def test_vm_cpu_accounting_changes_domain_at_paused_boundary(tmp_path):
    from clawbox.experiments.session_binding import PeerCpuAccounting
    group = tmp_path/'vm'
    group.mkdir()
    stat = group/'cpu.stat'
    stat.write_text('usage_usec 100\n')
    cpu = PeerCpuAccounting(('n0', 'n1'))
    cpu.register('session', 'runtime', group, 'n0')
    stat.write_text('usage_usec 400\n')
    cpu.handoff('session', 'n1')
    stat.write_text('usage_usec 900\n')
    cpu.register('session', 'runtime', group, 'n1')
    measured = cpu.sample()['nodes']
    assert measured['n0']['observed_cpu_seconds'] == .0004
    assert measured['n1']['observed_cpu_seconds'] == .0005
    stat.unlink()
    with pytest.raises(FileNotFoundError):
        cpu.sample()
    group.rmdir()
    assert cpu.sample()['nodes']['n1']['unobserved_exit_tails'] == 1
