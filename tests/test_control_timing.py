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
