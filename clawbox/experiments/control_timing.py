"""Per-callback monotonic spans, buffered until after critical sections exit."""
from contextlib import contextmanager
from collections import deque
from functools import wraps
from threading import local
import gc
import time

_state = local()
_gc_intervals = deque(maxlen=512)
_details_enabled = True
_WAITS = {'admission.condition_wait', 'admission.eviction_wait', 'placement.cpu_capacity_wait'}
_LOCK_WAITS = {'admission.lock.acquire', 'admission.condition.reacquire',
               'admission.lifecycle_reacquire'}
_LIGHT_STAGES = _WAITS | _LOCK_WAITS | {'callback', 'prepare.total'}


@contextmanager
def timing_details(enabled):
    """One worker process owns this setting, including its callback threads."""
    global _details_enabled
    previous, _details_enabled = _details_enabled, enabled
    try:
        yield
    finally:
        _details_enabled = previous


@contextmanager
def monitor_gc():
    """Record runtime pauses without tracing, logging, or locks inside GC."""
    if not _details_enabled:
        yield
        return
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
    if records is None or (not records.detailed and name not in _LIGHT_STAGES):
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
        self.detailed = _details_enabled

    def append(self, item):
        if len(self) == self.maxlen:
            self.dropped += 1
        super().append(item)


def _merged_intervals(intervals):
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a,b])
    return merged


def _resource_wait_seconds(records, start, end):
    waits = _merged_intervals([(max(start,s['start_ns']), min(end,s['end_ns'])) for s in records
             if s['stage'] in _WAITS and s['end_ns']>start and s['start_ns']<end])
    locks = _merged_intervals([(s['start_ns'],s['end_ns']) for s in records if s['stage'] in _LOCK_WAITS])
    i = j = overlap = 0
    while i < len(waits) and j < len(locks):
        a,b = waits[i]
        c,d = locks[j]
        overlap += max(0,min(b,d)-max(a,c))
        if b <= d:
            i += 1
        else:
            j += 1
    # Reacquiring an accounting lock is implementation overhead, even when
    # it occurs inside Condition.wait(). Nested lock spans count only once.
    return (sum(b-a for a,b in waits)-overlap)/1e9


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
        summary = {
            'duration_seconds': (root_span['end_ns']-root_span['start_ns'])/1e9,
            'thread_cpu_seconds': root_span['thread_cpu_ns']/1e9,
            'resource_queue_wait_seconds': _resource_wait_seconds(
                records, root_span['start_ns'], root_span['end_ns']),
        }
        # Queueing behind another admission or an ongoing eviction is not
        # proof of a memory shortage. Retain that distinction without full spans.
        wait_groups = {}
        for record in records:
            if record['stage'] not in _WAITS:
                continue
            reason = '+'.join(sorted(record.get('reasons', ()))) or record['stage']
            wait_groups.setdefault(reason, []).append(record)
        locks = [s for s in records if s['stage'] in _LOCK_WAITS]
        summary['queue_wait_seconds_by_reason'] = {
            reason: _resource_wait_seconds([*waits, *locks], root_span['start_ns'], root_span['end_ns'])
            for reason, waits in wait_groups.items()
        }
        prepare = next((s for s in records if s['stage']=='prepare.total'), None)
        if prepare is not None:
            summary['preparation_seconds'] = (prepare['end_ns']-prepare['start_ns'])/1e9
            summary['preparation_resource_queue_wait_seconds'] = _resource_wait_seconds(
                records, prepare['start_ns'], prepare['end_ns'])
        if records.detailed:
            summary.update(spans=list(records), gc_intervals=[
                s for s in tuple(_gc_intervals)
                if s['end_ns'] >= root_span['start_ns'] and s['start_ns'] <= root_span['end_ns']])
        emit({'event': event, 'session_id': session_id, **identity,
              'status': status, 'timing_details': records.detailed,
              'dropped_spans': records.dropped, **summary})


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
