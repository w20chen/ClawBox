import copy
from types import SimpleNamespace

import pytest

from clawbox.experiments.admission_forecast import AdmissionForecaster
from clawbox.experiments.knowledge.session_forecast import HostSessionStateManager, call_key
from clawtune_sidecar.signals import SignalHub
from clawtune_sidecar.signal_protocol import envelope
from clawtune_kb.contracts import validate


def setup_forecaster():
    now = [100.]
    load = dict(admission_queue_depth=2, tool_active_sessions=1,
        admission_queue_oldest_seconds=0., used_budget_ratio=.9,
        pending_budget_ratio=.2, free_floor_ratio=10.,
        admission_queue_last_blockers={'local_budget': 1})
    conditions = [0]
    f = AdmissionForecaster(clock=lambda: now[0])
    f.bind('s', domain='host', load=lambda: load.copy(), resident=lambda: True,
           stable=lambda: conditions[0])
    return f, now, load, conditions


def begin(f, execution, duration=10.):
    f.begin('s', {'execution_id': execution, 'prediction_receipt': execution})
    f.predicted('s', execution, duration=duration, memory=100, dependency_limited=False)


def train(f, now, waits=(10., 10., 10.)):
    for i, wait in enumerate(waits):
        key = 'history' + str(i)
        begin(f, key)
        # Real observations while waiting, not hindsight samples.
        for _ in range(int(wait)):
            now[0] += 1
            f.snapshot('s', [key])
        f.start('s', key)
        f.finish('s', key)
        now[0] += 10


def test_wait_is_added_and_execution_clock_starts_at_guest_boundary():
    f, now, _, _ = setup_forecaster()
    train(f, now)
    begin(f, 'tool')
    now[0] += 5
    value = f.snapshot('s', ['tool'])['tools']['tool']
    assert value['wait_remaining_ms'] == pytest.approx(5000)
    assert value['remaining_ms'] == pytest.approx(15000)  # full 10s execution still ahead
    assert value['sample_count'] == 3
    f.start('s', 'tool')
    now[0] += 2
    assert f.snapshot('s', ['tool'])['tools']['tool']['remaining_ms'] == pytest.approx(8000)
    now[0] += 9
    assert f.snapshot('s', ['tool'])['tools']['tool']['remaining_ms'] is None


def test_pressure_shift_and_cold_history_are_unknown():
    f, now, load, _ = setup_forecaster()
    begin(f, 'cold')
    assert f.snapshot('s', ['cold'])['tools']['cold']['remaining_ms'] is None
    f.finish('s', 'cold')
    train(f, now)
    load['admission_queue_depth'] = 1000
    load['admission_queue_last_blockers'] = {'emergency_free_floor': 1}
    now[0] += 2
    begin(f, 'different')
    assert f.snapshot('s', ['different'])['tools']['different']['remaining_ms'] is None


def test_wait_estimator_distinguishes_short_and_long_queues():
    f, now, load, _ = setup_forecaster()
    for depth in (1, 4, 12):
        load['admission_queue_depth'] = depth
        now[0] += 2
        train(f, now, (depth*2.,)*3)
    for depth in (1, 4, 12):
        load['admission_queue_depth'] = depth
        now[0] += 2
        begin(f, f'probe{depth}')
        value = f.snapshot('s', [f'probe{depth}'])['tools'][f'probe{depth}']
        assert value['wait_remaining_ms'] == pytest.approx(depth*2000.)
        f.finish('s', f'probe{depth}')


def test_pending_long_waits_are_censored_not_dropped():
    f, now, _, _ = setup_forecaster()
    train(f, now, (2., 2., 2.))
    for i in range(5):
        begin(f, f'pending{i}')
    now[0] += 20
    begin(f, 'new')
    # Five same-context waits have not ended after 20s. Three completed 2s
    # waits alone must not establish a 2s median for this population.
    value = f.snapshot('s', ['new'])['tools']['new']
    assert value['sample_count'] == 3
    assert value['remaining_ms'] is None


def test_conditions_change_permanently_withdraws_eta_and_finish_does_not_resurrect():
    f, now, _, conditions = setup_forecaster()
    begin(f, 'tool')
    f.start('s', 'tool')
    conditions[0] = 1
    assert f.snapshot('s', ['tool'])['tools']['tool']['reason'] == 'execution_conditions_changed'
    conditions[0] = 0
    assert f.snapshot('s', ['tool'])['tools']['tool']['remaining_ms'] is None
    f.finish('s', 'tool')
    f.start('s', 'tool')
    assert f.snapshot('s', ['tool'])['tools']['tool']['phase'] == 'finished'
    assert f.snapshot('s', ['tool'])['tools']['tool']['phase'] == 'unseen'
    assert f.snapshot('another', ['tool'])['tools']['tool']['phase'] == 'unseen'


def setup_session(tools=('a',), mode='parallel'):
    now = [100.]
    hub = SignalHub()
    manager = HostSessionStateManager(hub.publish, SimpleNamespace(), clock=lambda: now[0])
    manager.handoff_history[('gateway', 'runtime')].append(100.)
    message = envelope('session.observation', dict(revision=1, observer_id='observer', observer_started_at=1,
        session_key='session', parent_session_key=None, run_status='active', model_call_id=None,
        batch=dict(batch_id='batch', execution_mode=mode, mode_source='observed_sequence', tools=[
            dict(tool_call_id=t, started=True, settled=False, foreground=True,
                 predicted_duration_ms=10000, prediction_source='lattice') for t in tools])),
        source='plugin', identity=dict(gateway_id='gateway', runtime_id='runtime', session_id='session', run_id='run'))
    message['observed_at'] = now[0]
    manager.observe(message)
    return manager, now, message, hub


def test_host_eta_overrides_hook_start_and_live_progress_then_expires():
    m, now, message, hub = setup_session()
    owner = {**message['identity'], 'tool_call_id': 'a'}
    now[0] += 5
    m.host[call_key(owner, 'a')] = (105., 107., {'remaining_ms': 15000})
    m.progress(owner, {'remaining_duration_ms': 1}, 105., 110.)
    value = m.snapshots()[0]
    assert value['payload']['remaining_ms'] == pytest.approx(15100)
    now[0] += 2.1
    assert m.snapshots()[0]['payload']['remaining_ms'] is None
    assert m.snapshots()[0]['payload']['unavailable_reason'] == 'host_admission_forecast_stale'
    for row in hub.recent:
        validate(row, 'signal.schema.json')


def test_serial_waits_do_not_count_predecessors_twice():
    m, now, message, _ = setup_session(('a', 'b'), mode='sequential')
    for tool, remaining in (('a', 10000), ('b', 20000)):
        m.host[call_key(message['identity'], tool)] = (100., 102., {'remaining_ms': remaining})
    assert m.snapshots()[0]['payload']['remaining_ms'] == pytest.approx(20100)


def test_background_capable_tool_does_not_use_process_eta_for_logical_return():
    m, _, message, _ = setup_session()
    message['payload']['revision'] += 1
    message['payload']['batch']['tools'][0]['foreground'] = False
    m.host[call_key(message['identity'], 'a')] = (100., 102., {'remaining_ms': 10000})
    m.observe(message)
    assert m.snapshots()[0]['payload']['remaining_ms'] is None
    assert m.snapshots()[0]['payload']['unavailable_reason'] == 'logical_return_may_precede_execution_end'


def test_recovered_settled_snapshot_does_not_invent_completion_time():
    m, now, message, _ = setup_session()
    message['payload']['revision'] += 1
    message['payload']['batch']['tools'][0]['settled'] = True
    now[0] += 5
    message['observed_at'] = now[0]
    m.observe(message)
    assert m.snapshots()[0]['payload']['phase'] == 'HANDOFF'
    assert m.snapshots()[0]['payload']['remaining_ms'] is None


def test_handoff_calibration_uses_events_when_model_snapshot_arrives_first():
    m, now, message, _ = setup_session()
    m.tool_completed({**message['identity'], 'tool_call_id': 'a'}, 100.)
    changed = copy.deepcopy(message)
    changed['payload'].update(revision=2, batch=None, model_call_id='next')
    now[0] = 101.
    changed['observed_at'] = now[0]
    m.observe(changed)
    m.model_started(message['identity'], 'next', 100.05)
    assert list(m.handoff_history[('gateway', 'runtime')]) == pytest.approx([100., 50.])
    m.model_started(message['identity'], 'next', 101.)
    assert len(m.handoff_history[('gateway', 'runtime')]) == 2
