"""Read-only, online estimates of time from policy ingress to guest execution.

Learn residual waiting time at comparable queue/pressure observations. Pending
requests are right-censored evidence, not zero-duration or completed samples.
Nothing here grants capacity or assumes that process exit releases VM memory.
"""
from collections import deque
from bisect import bisect_left
from dataclasses import dataclass
from itertools import groupby
from operator import sub
import math
import threading
import time


@dataclass
class Waiting:
    session: str
    receipt: str
    arrived: float
    duration: float | None = None
    memory: float = 0.
    category: tuple | None = None
    # Observations change at most once per second per waiter. Keep immutable
    # snapshots so every forecast can share them outside the global lock.
    observations: tuple = ()
    started: float | None = None
    finished: bool = False
    invalid: bool = False
    conditions: object = None


class AdmissionForecaster:
    def __init__(self, *, clock=time.monotonic, history_size=512, min_samples=3):
        self.clock, self.min_samples = clock, min_samples
        self.history = deque(maxlen=history_size)
        self.pending = {}
        self.receipts = {}
        self.bindings = {}
        self.loads = {}
        self.loading = set()
        self.wait_cache = {}
        self.wait_cache_at = float('-inf')
        self.lock = threading.RLock()

    def bind(self, session, *, domain, load, resident, stable):
        with self.lock:
            self.bindings[session] = (domain, load, resident, stable)

    def begin(self, session, request, arrived=None):
        receipt = request.get('prediction_receipt')
        if not receipt or session not in self.bindings:
            return
        key = (session, request['execution_id'])
        with self.lock:
            if key not in self.pending:
                item = Waiting(session, receipt, self.clock() if arrived is None else arrived)
                self.pending[key] = item
                self.receipts[(session, receipt)] = item

    def predicted(self, session, execution, *, duration, memory, dependency_limited):
        with self.lock:
            item = self.pending.get((session, execution))
            if item is None or item.category is not None:
                return
            domain, _, resident, _ = self.bindings[session]
            domain = domain() if callable(domain) else domain
            item.duration = duration
            item.memory = memory
            item.category = (domain, not resident(), dependency_limited)
        self._observe(item)

    def _observe(self, item):
        # Never hold the forecasting lock while querying the admission ledger.
        binding = self.bindings.get(item.session)
        if binding is None or item.category is None:
            return None
        domain, load, _, _ = binding
        domain = domain() if callable(domain) else domain
        if item.category[0] != domain:
            return None  # migration changes the queue that owns this request
        now = self.clock()
        with self.lock:
            cached = self.loads.get(domain)
        if cached is None or now - cached[0] >= 1.:
            with self.lock:
                if domain in self.loading:
                    return None
                self.loading.add(domain)
            try:
                value = load()
                if value is None:
                    return None
                with self.lock:
                    active = [other for other in self.pending.values() if other.category
                              and other.category[0] == domain and other.started is not None]
                    value = {**value,
                        'active_work_seconds': sum(max(0., other.duration - (now-other.started))
                            for other in active if other.duration is not None),
                        'unknown_active': sum(other.duration is None or other.started + other.duration <= now
                                              for other in active)}
                    self.loads[domain] = (now, value)
            except (OSError, RuntimeError):
                return None  # Optional evidence must not fail command admission.
            finally:
                with self.lock:
                    self.loading.discard(domain)
        else:
            value = cached[1]
        # Ratios are measured occupancy / configured policy budget; these are
        # features, never claims about available physical memory.
        features = tuple(math.log1p(max(0., float(v))) for v in (
            now - item.arrived, item.memory,
            value['admission_queue_depth'], value['tool_active_sessions'],
            value['admission_queue_oldest_seconds'], value['used_budget_ratio'],
            value['pending_budget_ratio'], value['free_floor_ratio'],
            value['active_work_seconds'], value['unknown_active']))
        blockers = tuple(sorted(value['admission_queue_last_blockers']))
        sample = (now, features, blockers)
        with self.lock:
            if item.started is None and not item.finished:
                if not item.observations or now - item.observations[-1][0] >= 1.:
                    item.observations = (*item.observations[-31:], sample)
        return sample

    def start(self, session, execution):
        with self.lock:
            item = self.pending.get((session, execution))
            if item is not None and item.started is None and not item.finished:
                item.started = self.clock()
                item.conditions = self.bindings[session][3]()
                if item.observations:
                    self.history.append((item.category, item.observations, item.started))

    def finish(self, session, execution):
        with self.lock:
            item = self.pending.pop((session, execution), None)
            if item is not None:
                item.finished = True
                # Completion is not logical hook return. Keep a small tombstone
                # until the next polling request so it cannot look queued again.

    def discard(self, session):
        with self.lock:
            self.bindings.pop(session, None)
            self.pending = {k: v for k, v in self.pending.items() if k[0] != session}
            self.receipts = {k: v for k, v in self.receipts.items() if k[0] != session}

    @staticmethod
    def _distance(a, b):
        # Chebyshev distance squared: one square after the maximum is exactly
        # the same metric. C iterators avoid a Python generator/power per feature
        # in this heavily repeated forecast path.
        return max(map(abs, map(sub, a, b))) ** 2

    def _wait(self, item, observation):
        if observation is None:
            return None, 0
        now, features, blockers = observation
        cache_key = (item.category, tuple(round(f, 1) for f in features), blockers)
        neighbors = []
        with self.lock:
            if now - self.wait_cache_at >= 1.:
                self.wait_cache.clear()
                self.wait_cache_at = now
            elif cache_key in self.wait_cache:
                return self.wait_cache[cache_key]
            evidence = [(category, samples, end, True)
                        for category, samples, end in self.history if category == item.category]
            if len(evidence) < self.min_samples:
                return None, len(evidence)
            evidence += [(other.category, other.observations, now, False)
                         for other in self.pending.values()
                         if other is not item and other.category == item.category
                         and other.started is None and not other.finished]
        for category, samples, end, completed in evidence:
            if category != item.category or not samples:
                continue
            # Age is monotone within an execution. Compare adjacent ages instead
            # of rescanning every poll of every historical/live waiter.
            index = bisect_left(samples, features[0], key=lambda s: s[1][0])
            samples = samples[max(0, index-1):index+1]
            candidates = [(self._distance(features, f), end-t)
                          for t, f, b in samples if b == blockers and end > t]
            if candidates:
                distance, remaining = min(candidates)
                # No extrapolation to substantially different pressure/queue age.
                if distance <= .25:
                    neighbors.append((distance, remaining, completed))
        # Prefer the closest load regime. Mixing every admissible neighbor
        # would collapse short and long queues into a global average.
        radius = min((distance for distance, _, _ in neighbors), default=0.) + .01
        nearest = [row for row in neighbors if row[0] <= radius]
        count = sum(completed for _, _, completed in nearest)
        if count < self.min_samples:
            with self.lock:
                self.wait_cache[cache_key] = (None, count)
            return None, count
        # Kaplan-Meier median: ongoing long waits cannot bias estimates toward
        # the short requests that happened to finish first.
        survival, at_risk = 1., len(nearest)
        for duration, grouped in groupby(sorted(nearest, key=lambda row: row[1]), key=lambda row: row[1]):
            rows = [done for _, _, done in grouped]
            survival *= 1. - sum(rows) / at_risk
            if survival <= .5:
                with self.lock:
                    self.wait_cache[cache_key] = (duration, count)
                return duration, count
            at_risk -= len(rows)
        with self.lock:
            self.wait_cache[cache_key] = (None, count)
        return None, count

    def snapshot(self, session, receipts):
        result = {}
        for receipt in receipts:
            with self.lock:
                item = self.receipts.get((session, receipt))
            if item is None:
                result[receipt] = dict(phase='unseen', remaining_ms=None, reason='admission_not_observed')
                continue
            now = self.clock()
            count, wait = 0, None
            reason = None
            if item.finished:
                phase, remaining, reason = 'finished', None, 'awaiting_tool_return'
                with self.lock:
                    self.receipts.pop((session, receipt), None)
            elif item.started is not None:
                phase = 'executing'
                binding = self.bindings.get(session)
                conditions = binding[3]() if binding else None
                if conditions is None or conditions != item.conditions:
                    item.invalid = True
                remaining = None if item.duration is None else item.duration - (now-item.started)
                if item.invalid:
                    remaining, reason = None, 'execution_conditions_changed'
                elif remaining is None or remaining <= 0:
                    remaining, reason = None, 'execution_duration_unavailable_or_expired'
            else:
                phase = 'waiting'
                wait, count = self._wait(item, self._observe(item))
                remaining = wait + item.duration if wait is not None and item.duration is not None else None
                if remaining is None:
                    reason = 'admission_history_insufficient' if wait is None else 'duration_prediction_unavailable'
            result[receipt] = dict(phase=phase, remaining_ms=None if remaining is None else remaining*1000,
                wait_remaining_ms=None if wait is None else wait*1000, sample_count=count, reason=reason)
        return dict(tools=result, valid_for_ms=2000)
