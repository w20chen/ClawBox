"""Per-callback monotonic spans, buffered until after critical sections exit."""
from contextlib import contextmanager
from collections import deque
from functools import wraps
from threading import local
import gc
import time

_state = local()
_gc_intervals = deque(maxlen=512)


@contextmanager
def monitor_gc():
    """Record runtime pauses without tracing, logging, or locks inside GC."""
    started = {}

    def record(phase, info):
        if phase == 'start':
            started['at'] = time.monotonic_ns()
            started['cpu'] = time.thread_time_ns()
        elif 'at' in started:
            _gc_intervals.append({'generation': info['generation'],
                                  'start_ns': started.pop('at'),
                                  'end_ns': time.monotonic_ns(),
                                  'thread_cpu_ns': time.thread_time_ns() - started.pop('cpu'),
                                  'collected': info['collected'],
                                  'uncollectable': info['uncollectable']})

    gc.callbacks.append(record)
    try:
        yield
    finally:
        gc.callbacks.remove(record)


@contextmanager
def span(name):
    records = getattr(_state, 'records', None)
    if records is None:
        yield
        return
    started = time.monotonic_ns()
    cpu = time.thread_time_ns()
    try:
        yield
    finally:
        records.append({'stage': name, 'start_ns': started,
                        'end_ns': time.monotonic_ns(),
                        'thread_cpu_ns': time.thread_time_ns() - cpu})


@contextmanager
def timed_lock(lock, name):
    with span(name + '.wait'):
        lock.acquire()
    try:
        with span(name + '.hold'):
            yield
    finally:
        lock.release()


def operation(name):
    def decorate(callback):
        @wraps(callback)
        def wrapped(*args, **kwargs):
            with span(name):
                return callback(*args, **kwargs)
        return wrapped
    return decorate


def trace_callback(event, emit, session_id):
    def decorate(callback):
        @wraps(callback)
        def wrapped(request):
            previous = getattr(_state, 'records', None)
            records = _state.records = []
            status = 'error'
            try:
                with span('callback'):
                    result = callback(request)
                status = 'ok'
                return result
            finally:
                _state.records = previous
                callback_span = next(s for s in reversed(records) if s['stage'] == 'callback')
                pauses = [s for s in tuple(_gc_intervals)
                          if s['end_ns'] >= callback_span['start_ns']
                          and s['start_ns'] <= callback_span['end_ns']]
                emit({'event': event, 'session_id': session_id,
                      'execution_id': request.get('execution_id'),
                      'status': status, 'spans': records, 'gc_intervals': pauses})
        return wrapped
    return decorate
