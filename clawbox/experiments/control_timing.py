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
def span(name, **attributes):
    records = getattr(_state, 'records', None)
    if records is None:
        yield attributes
        return
    started = time.monotonic_ns()
    cpu = time.thread_time_ns()
    status = 'error'
    try:
        yield attributes
        status = 'ok'
    finally:
        records.append({'stage': name, 'start_ns': started,
                        'end_ns': time.monotonic_ns(),
                        'thread_cpu_ns': time.thread_time_ns() - cpu,
                        'status': status, **attributes})


class _Records(deque):
    def __init__(self):
        super().__init__(maxlen=8192)
        self.dropped = 0

    def append(self, item):
        if len(self) == self.maxlen:
            self.dropped += 1
        super().append(item)


@contextmanager
def capture_spans():
    """Capture a lifecycle stage, also retaining it in an enclosing callback."""
    previous = getattr(_state, 'records', None)
    records = _state.records = _Records()
    try:
        yield records
    finally:
        _state.records = previous
        if previous is not None:
            for record in records:
                previous.append(record)


@contextmanager
def trace_scope(event, emit, session_id, **identity):
    """Buffer one operation; callers must exit owned locks before this scope."""
    previous = getattr(_state, 'records', None)
    records = _state.records = _Records()
    status = 'error'
    try:
        with span('callback') as root:
            yield
        status = 'ok'
    finally:
        _state.records = previous
        root_span = records[-1]
        pauses = [s for s in tuple(_gc_intervals)
                  if s['end_ns'] >= root_span['start_ns']
                  and s['start_ns'] <= root_span['end_ns']]
        emit({'event': event, 'session_id': session_id, **identity,
              'status': status, 'spans': list(records),
              'dropped_spans': records.dropped, 'gc_intervals': pauses})


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
            with trace_scope(event, emit, session_id,
                             execution_id=request.get('execution_id')):
                return callback(request)
        return wrapped
    return decorate
