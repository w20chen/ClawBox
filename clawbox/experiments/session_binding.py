"""Run-owned authority for a paused Runtime/Tool pair.

The binding is independent of the current CPU lease and native sandbox IDs.
Only a successful native Runtime restore commits a prepared destination; an
ambiguous restore fences the pair and never launches a second copy at home.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any
import math
import time
import uuid
from pathlib import Path


@dataclass
class SessionBinding:
    session_id: str
    index: int
    current: str
    domains: dict[str, Any]
    epoch: int = 0
    transition: str = 'stable'
    pending: str | None = None
    transaction_id: str | None = None
    lifecycles: dict[str, Any] = field(default_factory=dict)
    lock: Any = field(default_factory=RLock, repr=False)

    @property
    def domain(self):
        return self.domains[self.current]

    @property
    def restore_domain(self):
        return self.domains[self.pending or self.current]

    def begin(self, target: str) -> tuple[int, str]:
        with self.lock:
            if self.transition != 'stable':
                raise RuntimeError(f'session is {self.transition}: {self.session_id}')
            if target not in self.domains:
                raise ValueError(f'unknown target domain: {target}')
            if set(self.lifecycles) != {'runtime', 'tool'} or any(
                    getattr(getattr(vm, '_state', None), 'value', getattr(vm, '_state', None)) != 'swapped'
                    for vm in self.lifecycles.values()):
                raise RuntimeError('migration requires both roles SWAPPED')
            self.domain.coordinator.claim_migration(self.session_id)
            self.pending = target
            self.transaction_id = uuid.uuid4().hex
            self.transition = 'staging'
            return self.epoch, self.transaction_id

    def mark_restoring(self, epoch: int, transaction_id: str) -> None:
        with self.lock:
            self._check(epoch, transaction_id)
            if self.transition != 'staging':
                raise RuntimeError('replica is not in staging')
            self.transition = 'restoring'

    def commit(self, epoch: int, transaction_id: str, *, memory_samples=None) -> None:
        with self.lock:
            self._check(epoch, transaction_id)
            if self.transition != 'restoring' or not self.lifecycles['runtime'].resident:
                raise RuntimeError('authority requires successful native Runtime restore')
            source = self.domain.coordinator
            destination = self.restore_domain.coordinator
            with source.demand.condition:
                source.handoff(self.session_id, destination, memory_samples=memory_samples)
                self.current = self.pending
                self.epoch += 1
                self.pending = None
                self.transition = 'stable'
                self.transaction_id = None
                destination.release_migration(self.session_id)

    def fail(self, epoch: int, transaction_id: str, *, known_paused: bool) -> None:
        with self.lock:
            self._check(epoch, transaction_id)
            if not known_paused:
                self.transition = 'unknown'
                return  # Retain the migration fence: a second resume is forbidden.
            if any(getattr(getattr(vm, '_state', None), 'value', getattr(vm, '_state', None)) != 'swapped'
                   for vm in self.lifecycles.values()):
                raise RuntimeError('cannot roll back an unconfirmed paused pair')
            self.domain.coordinator.release_migration(self.session_id)
            self.pending = None
            self.transition = 'stable'
            self.transaction_id = None

    def _check(self, epoch, transaction_id):
        if epoch != self.epoch or transaction_id != self.transaction_id:
            raise RuntimeError('stale session binding epoch or transaction')

    def event(self, name: str, **values):
        return {'event': name, 'session_id': self.session_id, 'epoch': self.epoch,
                'source_domain': self.current, 'target_domain': self.pending,
                'transaction_id': self.transaction_id, 'transition': self.transition,
                'monotonic_s': time.monotonic(), 'unix_s': time.time(), **values}


def choose_resume(home: str, candidates: dict[str, dict], *, policy: str,
                  threshold_seconds: float) -> tuple[str, dict]:
    """Rank one common feasibility set; never convert unknown costs to zero."""
    if policy not in {'home', 'load-aware', 'cost-aware'}:
        raise ValueError(f'unknown resume policy: {policy}')
    if threshold_seconds < 0:
        raise ValueError('migration threshold must be nonnegative')
    if home not in candidates:
        raise ValueError('resume candidates must include home')
    scores = {}
    for node, evidence in candidates.items():
        wait = evidence.get('resource_wait_seconds')
        artifact = evidence.get('artifact_seconds')
        native = evidence.get('native_restore_seconds')
        eligible = evidence.get('eligible', False) and evidence.get('artifacts_available', True)
        if not eligible:
            score = math.inf
        elif policy == 'load-aware':
            # Unknown blocked waits sort after immediately available nodes;
            # the numerical infinity is a ranking sentinel, never a prediction.
            score = float(wait) if wait is not None else math.inf
        elif policy == 'cost-aware':
            score = (float(wait) + float(artifact) + float(native)
                     if all(value is not None for value in (wait, artifact, native)) else math.inf)
        else:
            score = 0.0 if node == home else math.inf
        scores[node] = score
    best = min(scores, key=lambda node: (scores[node], node != home, node))
    if policy == 'cost-aware':
        # A missing home estimate is not evidence that migration is profitable.
        if not math.isfinite(scores[home]) or scores[home] - scores[best] <= threshold_seconds:
            best = home
    if policy == 'home' or not math.isfinite(scores[best]):
        best = home
    return best, {'policy': policy, 'candidates': candidates,
                  'predicted_ready_seconds': {key: value if math.isfinite(value) else None
                                              for key, value in scores.items()},
                  'selected_domain': best, 'migration_threshold_seconds': threshold_seconds,
                  'cost_dependencies': 'resource_then_transfer_then_native'}


class PeerCpuAccounting:
    """Observed CPU counters from verified experiment VM cgroups, by domain.

    Registration and migration sample boundaries explicitly. A disappearing
    cgroup tail is unavailable, so these per-node totals are labelled lower
    bounds rather than filled with an invented final counter.
    """
    def __init__(self, domains):
        self.lock = RLock()
        self.groups = {}
        self.cpu_usec = {node: 0 for node in domains}
        self.lost_tails = {node: 0 for node in domains}
        self.unknown_intervals = {node: 0 for node in domains}

    @staticmethod
    def _counter(path):
        values = dict(line.split() for line in (path / 'cpu.stat').read_text().splitlines())
        current = int(values['usage_usec'])
        if current < 0:
            raise ValueError('negative VM cgroup CPU counter')
        return current

    def _read(self, key, entry):
        node, path, identity, previous = entry
        try:
            stat = path.stat()
            if (stat.st_dev, stat.st_ino) != identity:
                self.lost_tails[node] += 1
                self.groups.pop(key, None)
                return
            current = self._counter(path)
            if current < previous:
                raise ValueError('VM cgroup CPU counter decreased')
            self.cpu_usec[node] += current - previous
            self.groups[key] = node, path, identity, current
        except FileNotFoundError:
            if path.exists():
                self.unknown_intervals[node] += 1
            else:
                self.lost_tails[node] += 1
            self.groups.pop(key, None)
        except (OSError, ValueError, KeyError):
            self.unknown_intervals[node] += 1
            self.groups.pop(key, None)

    def register(self, session, role, path, node):
        path = Path(path)
        key = session, role
        with self.lock:
            old = self.groups.get(key)
            if old is not None:
                self._read(key, old)
            try:
                stat = path.stat()
                identity = stat.st_dev, stat.st_ino
                baseline = self._counter(path)
            except (OSError, ValueError, KeyError):
                self.unknown_intervals[node] += 1
                self.groups.pop(key, None)
                return
            previous = self.groups.get(key)
            if previous is None or previous[2] != identity:
                # Native CleanForReuse keeps the leaf and its cumulative CPU
                # counter. CPU before this observation belongs to an unknown
                # prior lease, not automatically to this experiment VM.
                self.groups[key] = node, path, identity, baseline
            else:
                self.groups[key] = node, path, identity, previous[3]
            self._read(key, self.groups[key])

    def handoff(self, session, target):
        with self.lock:
            for key, entry in list(self.groups.items()):
                if key[0] != session:
                    continue
                self._read(key, entry)
                if key in self.groups:
                    _, path, identity, previous = self.groups[key]
                    self.groups[key] = target, path, identity, previous

    def sample(self):
        with self.lock:
            for key, entry in list(self.groups.items()):
                self._read(key, entry)
            return {'measurement': 'verified_vm_cgroup_cpu_stat',
                    'scope': 'runtime_and_tool_cgroups', 'coverage': 'sampled_lower_bound',
                    'monotonic_s': time.monotonic(),
                    'nodes': {node: {'observed_cpu_seconds': value / 1e6,
                                    'unobserved_exit_tails': self.lost_tails[node],
                                    'unobserved_counter_intervals': self.unknown_intervals[node]}
                              for node, value in self.cpu_usec.items()}}
