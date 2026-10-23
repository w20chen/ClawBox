"""Atomic physical-demand accounting shared by every compute domain.

Reservations cover only growth not already visible in the measured charge.
CPU placement, runtime congestion enforcement and snapshot policy are external.
"""
from dataclasses import dataclass
from threading import Condition
from typing import Callable
from pathlib import Path
import os
import time


@dataclass
class Claim:
    scope: str
    amount: int
    local_amount: int
    sample: Callable[[], tuple[int, int]] | None
    baseline: tuple[int, int]

    def remaining(self):
        if self.sample is None:
            return self.amount, self.local_amount
        total, local = self.sample()
        return (max(0, self.amount - max(0, total - self.baseline[0])),
                max(0, self.local_amount - max(0, local - self.baseline[1])))


class DemandLedger:
    def __init__(self, *, sample: Callable[[], tuple[int, int]], capacity: int,
                 free_floor: int = 0, margin: int = 0, cgroup: Path | None = None,
                 exit_ratio: float = .8):
        if capacity <= 0:
            raise ValueError('physical demand capacity must be positive')
        self.condition = Condition()
        self.sample = sample
        self.capacity = capacity
        self.free_floor = free_floor
        self.margin = margin
        self.cgroup = cgroup
        self.exit_ratio = exit_ratio
        self.congested = False
        self.last_severe_at = None
        self.sample_severe = False
        self.previous = None
        self.check = lambda: None
        self.claims: dict[tuple[str, str], Claim] = {}
        self.pending_upper_bound_fast_path_count = 0
        self.pending_exact_count = 0
        # Exact claim reads can touch hundreds of VM cgroup files.  The policy
        # condition protects admission decisions, but holding it across those
        # reads prevents completed tools from releasing their claims and turns
        # a conservative near-capacity check into a global lock convoy.  One
        # caller samples while concurrent exact checks share that result; the
        # policy condition is reacquired before the result is reconciled with
        # claims added or removed during the read.
        self._exact_sample_condition = Condition()
        self._exact_sample_inflight = False
        self._exact_sample_result: dict[
            tuple[str, str], tuple[Claim, tuple[int, int] | BaseException]
        ] = {}

    def pending_upper_bound(self, scope=None):
        """Return the safe nominal bound without sampling every live claim."""
        return sum(c.amount if scope is None else c.local_amount
                   for c in self.claims.values()
                   if scope is None or c.scope == scope)

    def pending(self, scope=None):
        # Callers hold the shared condition throughout sample/check/commit.
        self.pending_exact_count += 1
        return sum(c.remaining()[0 if scope is None else 1]
                   for c in self.claims.values() if scope is None or c.scope == scope)

    def pending_breakdown_without_condition(self, scope: str) -> tuple[int, int]:
        """Return global and scoped pending demand without pinning admission.

        The caller must hold ``self.condition``.  Claims created while the
        filesystem reads are in flight are charged at their full nominal
        amount; removed or replaced claims are ignored.  This preserves the
        same safe reservation semantics while allowing tool completion and
        unrelated fast-path admissions to publish progress.
        """
        snapshot = dict(self.claims)
        self.pending_exact_count += 1
        leader = False
        measured: dict[
            tuple[str, str], tuple[Claim, tuple[int, int] | BaseException]
        ]
        self.condition.release()
        try:
            with self._exact_sample_condition:
                if self._exact_sample_inflight:
                    self._exact_sample_condition.wait_for(
                        lambda: not self._exact_sample_inflight
                    )
                    measured = dict(self._exact_sample_result)
                else:
                    self._exact_sample_inflight = True
                    leader = True
                    measured = {}
            if leader:
                for key, claim in snapshot.items():
                    try:
                        value: tuple[int, int] | BaseException = claim.remaining()
                    except BaseException as exc:
                        value = exc
                    measured[key] = (claim, value)
                with self._exact_sample_condition:
                    self._exact_sample_result = measured
                    self._exact_sample_inflight = False
                    self._exact_sample_condition.notify_all()
        finally:
            if leader:
                # Never strand followers if an unexpected interpreter-level
                # failure occurs outside an individual claim read.
                with self._exact_sample_condition:
                    if self._exact_sample_inflight:
                        self._exact_sample_inflight = False
                        self._exact_sample_condition.notify_all()
            self.condition.acquire()

        global_pending = 0
        scoped_pending = 0
        for key, claim in self.claims.items():
            sampled = measured.get(key)
            if sampled is None or sampled[0] is not claim:
                remaining = (claim.amount, claim.local_amount)
            else:
                value = sampled[1]
                if isinstance(value, BaseException):
                    raise value
                remaining = value
            global_pending += remaining[0]
            if claim.scope == scope:
                scoped_pending += remaining[1]
        return global_pending, scoped_pending

    def reasons(self, additional: int, margin: int = 0, *,
                pending: int | None = None,
                sampled: tuple[int, int] | None = None):
        self.check()
        used, available = sampled if sampled is not None else self.sample()
        reasons = []
        committed = used + (self.pending() if pending is None else pending) + self.margin + margin
        if committed + additional > self.capacity:
            reasons.append('global_memory_budget')
        severe = self._severe(used)
        if severe:
            self.congested = True
            self.last_severe_at = time.monotonic()
        elif self.congested and (committed <= self.capacity*self.exit_ratio
                or (self.last_severe_at is not None and time.monotonic()-self.last_severe_at >= 1.0)):
            # A VM can retain its backing after reclaim stalls cease. Do not
            # require backing release forever: after a quiet second, allow
            # requests that still pass the independent demand/capacity check.
            self.congested = False
        if self.congested:
            reasons.append('sustained_memory_congestion')
        if available < self.free_floor:
            reasons.append('host_free_floor')
        return tuple(reasons)

    def _severe(self, used):
        if self.cgroup is None:
            return False
        stats = dict(line.split() for line in (self.cgroup/'memory.stat').read_text().splitlines())
        psi = None
        path = self.cgroup/'memory.pressure'
        if path.exists():
            some = next(line for line in path.read_text().splitlines() if line.startswith('some '))
            psi = int(dict(field.split('=') for field in some.split()[1:])['total'])
        now = time.monotonic()
        sample = (now, int(stats.get('pgscan', 0)), int(stats.get('pgsteal', 0)), psi)
        previous = self.previous
        if previous is None:
            self.previous = sample
            return False
        dt = now-previous[0]
        # Do not let rapid concurrent admission requests erase the interval.
        if dt < .1:
            return self.sample_severe
        self.previous = sample
        scanned, stolen = sample[1]-previous[1], sample[2]-previous[2]
        stalled = psi is not None and previous[3] is not None and (psi-previous[3])/1e6/dt >= .1
        inefficient = (scanned > 0 and scanned*os.sysconf('SC_PAGE_SIZE')/dt >= 64*1024**2
                       and stolen/scanned < .1)
        self.sample_severe = used >= .9*self.capacity and (stalled or inefficient)
        return self.sample_severe

    def add(self, key, *, scope, amount, local_amount, sample=None):
        if key in self.claims:
            raise RuntimeError(f'duplicate demand claim: {key}')
        self.claims[key] = Claim(scope, amount, local_amount, sample,
                                 sample() if sample else (0, 0))

    def extend(self, key, *, amount, local_amount):
        """Add one execution reservation to an existing aggregate claim."""
        claim = self.claims.get(key)
        if claim is None:
            raise RuntimeError(f'missing demand claim: {key}')
        claim.amount += amount
        claim.local_amount += local_amount

    def release_amount(self, key, *, amount, local_amount):
        """Release one execution without dropping sibling reservations."""
        claim = self.claims.get(key)
        if claim is None:
            raise RuntimeError(f'missing demand claim: {key}')
        if amount < 0 or local_amount < 0:
            raise ValueError('released demand must not be negative')
        if amount > claim.amount or local_amount > claim.local_amount:
            raise RuntimeError(f'demand release exceeds claim: {key}')
        claim.amount -= amount
        claim.local_amount -= local_amount
        if claim.amount == 0 and claim.local_amount == 0:
            self.claims.pop(key, None)
        self.condition.notify_all()

    def release(self, key):
        self.claims.pop(key, None)
        self.condition.notify_all()

    def rebase(self, key):
        # A restored VM's resident base is a lifecycle allocation, not execution
        # growth. Start the tool growth observation after restoration completes.
        claim = self.claims.get(key)
        if claim and claim.sample:
            claim.baseline = claim.sample()
