"""Reentrant admission lock with bounded priority for short progress updates.

Memory checks remain atomic with reservation commits. Completion does not queue
behind every expensive memory check; at most eight priority handoffs precede a
waiting ordinary caller. Only the selected waiter is awakened at each handoff.
"""
from collections import deque
from dataclasses import dataclass, field
from threading import Event, Lock, get_ident


@dataclass(eq=False)
class _Waiter:
    owner: int
    ready: Event = field(default_factory=Event)
    granted: bool = False


class AdmissionLock:
    def __init__(self):
        self._guard = Lock()
        self._owner = None
        self._depth = 0
        self._normal = deque()
        self._progress = deque()
        self._burst = 0

    def acquire(self, blocking=True, timeout=-1, *, progress=False):
        if not blocking and timeout != -1:
            raise ValueError('cannot specify timeout for a non-blocking acquire')
        owner = get_ident()
        with self._guard:
            if self._owner == owner:
                self._depth += 1
                return True
            if self._owner is None:
                self._owner, self._depth = owner, 1
                return True
            if not blocking:
                return False
            waiter = _Waiter(owner)
            queue = self._progress if progress else self._normal
            queue.append(waiter)
        if waiter.ready.wait(None if timeout == -1 else timeout):
            return True
        with self._guard:
            if waiter.granted:
                return True
            queue.remove(waiter)
            return False

    def _handoff(self):
        if self._progress and (not self._normal or self._burst < 8):
            waiter = self._progress.popleft()
            self._burst += 1
        elif self._normal:
            waiter = self._normal.popleft()
            self._burst = 0
        else:
            self._owner, self._depth, self._burst = None, 0, 0
            return
        self._owner, self._depth = waiter.owner, 1
        waiter.granted = True
        waiter.ready.set()

    def release(self):
        with self._guard:
            if self._owner != get_ident():
                raise RuntimeError('cannot release unowned admission lock')
            self._depth -= 1
            if self._depth == 0:
                self._handoff()

    def _is_owned(self):
        with self._guard:
            return self._owner == get_ident()

    def _release_save(self):
        with self._guard:
            if self._owner != get_ident():
                raise RuntimeError('cannot release unowned admission lock')
            state = self._depth, self._owner
            self._handoff()
            return state

    def _acquire_restore(self, state):
        self.acquire()
        with self._guard:
            self._depth = state[0]

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class ProgressLock:
    """Priority entrance to the same lock, not a separate accounting domain."""
    def __init__(self, lock):
        self.lock = lock

    def acquire(self):
        return self.lock.acquire(progress=True)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()
