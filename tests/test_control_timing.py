from threading import Lock

import pytest

from clawbox.experiments.control_timing import span, timed_lock, trace_callback


def test_timing_keeps_nested_spans_and_emits_after_unlock():
    events = []
    lock = Lock()

    def emit(event):
        assert lock.acquire(blocking=False)
        lock.release()
        events.append(event)

    @trace_callback('completed', emit, 's')
    def callback(request):
        with timed_lock(lock, 'accounting'):
            with span('sample'):
                return {'status': 'COMPLETED'}

    assert callback({'execution_id': 'e'}) == {'status': 'COMPLETED'}
    assert len(events) == 1
    event = events[0]
    assert event['execution_id'] == 'e'
    spans = {s['stage']: s for s in event['spans']}
    assert spans.keys() == {'accounting.wait', 'accounting.hold', 'sample', 'callback'}
    assert spans['callback']['start_ns'] <= spans['sample']['start_ns']
    assert spans['sample']['end_ns'] <= spans['callback']['end_ns']
    assert all(s['thread_cpu_ns'] >= 0 for s in spans.values())


def test_failed_callback_preserves_error_and_resets_timing_context():
    events = []

    @trace_callback('completed', events.append, 's')
    def callback(request):
        with span('failed'):
            raise ValueError('original')

    with pytest.raises(ValueError, match='original'):
        callback({'execution_id': 'e'})
    assert events[0]['status'] == 'error'
    before = list(events[0]['spans'])
    with span('outside'):
        pass
    assert events[0]['spans'] == before


def test_gc_intervals_are_scoped_and_correlated_without_logging_in_gc():
    import gc
    from clawbox.experiments.control_timing import monitor_gc
    before = list(gc.callbacks)
    events = []

    @trace_callback('completed', events.append, 's')
    def callback(request):
        gc.collect()

    with monitor_gc():
        callback({'execution_id': 'gc'})
    assert gc.callbacks == before
    assert any(s['generation'] == 2 for s in events[0]['gc_intervals'])
    assert all(s['end_ns'] >= s['start_ns'] for s in events[0]['gc_intervals'])


def test_condition_reacquisition_is_separate_from_condition_sleep():
    from threading import Condition
    from clawbox.experiments.admission_lock import AdmissionLock
    events = []
    condition = Condition(AdmissionLock())

    @trace_callback('admission', events.append, 's')
    def callback(request):
        with condition:
            with condition:
                with span('condition_wait', reasons=['configured_memory_budget']):
                    condition.wait(0.001)
                assert condition._is_owned()

    callback({'execution_id': 'e'})
    spans = events[0]['spans']
    wait = next(s for s in spans if s['stage'] == 'condition_wait')
    reacquire = next(s for s in spans if s['stage'] == 'admission.condition.reacquire')
    assert wait['start_ns'] <= reacquire['start_ns'] <= reacquire['end_ns'] <= wait['end_ns']
    assert wait['reasons'] == ['configured_memory_budget']
    assert wait['status'] == 'ok'


def test_bounded_trace_retains_root_and_reports_missing_spans():
    from clawbox.experiments.control_timing import trace_scope
    events = []
    with trace_scope('collection', events.append, 's'):
        for _ in range(8200):
            with span('file'):
                pass
    assert len(events[0]['spans']) == 8192
    assert events[0]['dropped_spans'] == 9
    assert events[0]['spans'][-1]['stage'] == 'callback'
