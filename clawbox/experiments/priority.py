"""Stable experiment placement intent over CubeSandbox-owned VM cgroups.

The root-owned registry conveys an opaque capability through Cube's persisted
environment. CubeSandbox keeps allocating and recycling its normal fast-pool
cgroups; it publishes the assigned leaf back through that capability so the
experiment can verify and control the official VM cgroup.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import time
from pathlib import Path
from contextlib import ExitStack, contextmanager
from threading import RLock
from .admission_lock import AdmissionLock, ProgressLock
from .control_timing import timed_lock, operation
import uuid

REGISTRY = Path('/run/clawbox/cgroups')
TOKEN_ENV = 'CLAWBOX_CGROUP_TOKEN'
OFFICIAL_CGROUP = re.compile(r'^/cube_sandbox/sandbox/[0-9]+$')
PLACEMENT_SAMPLE_MIN_BYTES = 8 * 1024 * 1024


def c2_applies(execution_scope: str | None, operation: str | None) -> bool:
    """Return whether one admitted operation is eligible for C2 placement."""
    return execution_scope == 'agent-tool' and operation == 'exec'


def _format_cpu_list(cpus) -> str:
    ordered = sorted(set(cpus))
    ranges = []
    for cpu in ordered:
        if not ranges or cpu != ranges[-1][-1] + 1:
            ranges.append([cpu, cpu])
        else:
            ranges[-1][-1] = cpu
    return ','.join(str(first) if first == last else f'{first}-{last}'
                    for first, last in ranges)


def physical_cluster_groups(cpus: str, *, min_physical_cores: int,
                            include_smt: bool = False,
                            sysfs: Path = Path('/sys/devices/system/cpu')) -> tuple[str, ...]:
    """Return complete Kunpeng cluster masks for the permitted CPU pool.

    Linux reports the outer L3 sharing set in cache ``shared_cpu_list`` and
    the scheduling/LLC cluster in ``cluster_cpus_list``.  A candidate retains
    the complete cluster and is accepted only when the permitted CPU pool
    covers every physical core in it. By default the mask uses one permitted
    representative per core; ``include_smt`` retains all permitted siblings.
    """
    from .topology import parse_cpu_list
    allowed = parse_cpu_list(cpus)
    seen_clusters: set[tuple[tuple[int, ...], tuple[int, ...]]] = set()
    groups = []
    for cpu in sorted(allowed):
        topology = sysfs / f'cpu{cpu}' / 'topology'
        cache_root = sysfs / f'cpu{cpu}' / 'cache'
        cache_candidates = []
        try:
            indexes = tuple(cache_root.glob('index*'))
        except OSError:
            indexes = ()
        for index in indexes:
            try:
                cache_type = (index/'type').read_text().strip()
                level = int((index/'level').read_text().strip())
                shared = tuple(sorted(parse_cpu_list((index/'shared_cpu_list').read_text())))
            except (OSError, ValueError):
                continue
            if cache_type in {'Unified', 'Data'} and shared:
                cache_candidates.append((level, cache_type == 'Unified', shared))
        if not cache_candidates:
            raise RuntimeError(f'CPU {cpu} has no readable LLC sharing metadata')
        llc = max(cache_candidates)[2]
        try:
            cluster = tuple(sorted(parse_cpu_list((topology/'cluster_cpus_list').read_text())))
        except OSError as exc:
            raise RuntimeError(
                f'CPU {cpu} has no readable LLC cluster metadata'
            ) from exc
        key = llc, cluster
        if key in seen_clusters:
            continue
        seen_clusters.add(key)
        if not cluster or not set(cluster) <= set(llc):
            raise RuntimeError(
                f'CPU cluster {cluster} is not contained in reported LLC {llc}'
            )
        physical_cores = set()
        representatives = []
        logical_members = []
        complete = True
        for member in cluster:
            try:
                siblings = tuple(sorted(parse_cpu_list(
                    (sysfs/f'cpu{member}'/'topology'/'thread_siblings_list').read_text()
                )))
            except OSError as exc:
                raise RuntimeError(
                    f'CPU {member} has no readable SMT sibling metadata'
                ) from exc
            if not siblings or siblings in physical_cores:
                continue
            physical_cores.add(siblings)
            permitted = set(siblings) & allowed
            if not permitted:
                complete = False
                break
            representatives.append(min(permitted))
            logical_members.extend(sorted(permitted))
        if complete and len(representatives) >= min_physical_cores:
            groups.append(_format_cpu_list(logical_members if include_smt else representatives))
    if not groups:
        raise RuntimeError(
            'no complete LLC cluster with at least '
            f'{min_physical_cores} permitted physical cores exists in CPUs {cpus}'
        )
    return tuple(groups)


def _pressure_total(path: Path) -> int:
    for line in path.read_text().splitlines():
        fields = line.split()
        if fields and fields[0] == 'some':
            for field in fields[1:]:
                key, separator, value = field.partition('=')
                if separator and key == 'total':
                    return int(value)
    raise RuntimeError(f'cpu pressure lacks some total: {path}')


def _cpu_times(path: Path) -> dict[int, tuple[int, int]]:
    """Read per-CPU busy and total ticks from procfs."""
    result = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if not fields or not re.fullmatch(r'cpu\d+', fields[0]):
            continue
        values = [int(value) for value in fields[1:]]
        total = sum(values)
        idle = sum(values[3:5])
        result[int(fields[0][3:])] = total-idle, total
    return result


def session_priority(index: int, concurrency: int, repetition: int) -> str:
    # Exactly one HIGH session, balanced across paired repetitions. No arm ID,
    # policy, prediction or thread arrival order participates in this mapping.
    return 'high' if index == repetition % concurrency else 'low'


class PriorityDomain:
    leaf_release_grace_seconds = 5.0
    leaf_kill_grace_seconds = 5.0

    def __init__(self, parent: Path, *, concurrency: int, repetition: int,
                 registry: Path = REGISTRY, placements: dict | None = None,
                 journal_directory: Path | None = None, legacy: bool = True,
                 topology_mode: str = 'c0',
                 topology_pressure_ratio: float = .10,
                 topology_pressure_samples: int = 2,
                 topology_group_max_active: int = 2,
                 topology_min_duration_seconds: float = 1.0,
                 topology_include_smt: bool = False,
                 emit=None, proc_stat: Path = Path('/proc/stat'),
                 cgroup_mount: Path = Path('/sys/fs/cgroup')):
        self.parent = parent.resolve()
        self.cgroup_mount = cgroup_mount.resolve()
        self.legacy = legacy
        self.registry = registry
        self.placements = placements or {}
        self.observations = []
        self.topology_mode = topology_mode
        self.topology_pressure_ratio = topology_pressure_ratio
        self.topology_pressure_samples = topology_pressure_samples
        self.topology_group_max_active = topology_group_max_active
        self.topology_min_duration_seconds = topology_min_duration_seconds
        self.topology_include_smt = topology_include_smt
        self.emit = emit or (lambda event: None)
        self._topology_lock = AdmissionLock()
        self._topology_progress_lock = ProgressLock(self._topology_lock)
        self._topology_sessions = {i: RLock() for i in range(concurrency)}
        self._cpu_sample_at = 0.0
        self._topology_stopping = False
        self._group_load: dict[str, int] = {}
        self._groups_by_pool: dict[tuple[str, int, bool], tuple[str, ...]] = {}
        self._preferred_groups: dict[tuple[int, str], str] = {}
        self._vcpu_threads: dict[tuple[int, str], tuple[tuple[int, str], ...]] = {}
        self._full_masks: dict[tuple[int, str], str] = {}
        self._fine_masks: dict[tuple[int, str], str] = {}
        self._expanded: set[tuple[int, str]] = set()
        self._pressure_previous: dict[tuple[int, str], tuple[float, int]] = {}
        self._pressure_streak: dict[tuple[int, str], int] = {}
        self._candidate_groups: set[str] = set()
        self._latest_group_utilization: dict[str, float | None] = {}
        self._proc_stat = proc_stat
        if topology_mode == 'c2':
            try:
                self._previous_cpu_times = _cpu_times(proc_stat)
            except OSError:
                self._previous_cpu_times = {}
        else:
            self._previous_cpu_times = {}
        self.topology_events: list[dict] = []
        self._call_keys: dict[str, tuple[int, str]] = {}
        self._active_calls: dict[tuple[int, str], set[str]] = {}
        self._pending_started: dict[tuple[int, str], float] = {}
        self._pending_vcpus: dict[tuple[int, str], int] = {}
        self._capacity_bypassed: set[tuple[int, str]] = set()
        self._initial_residency_verified: set[str] = set()
        self._total_call_assignments = 0
        self._total_expansions = 0
        self._total_capacity_bypasses = 0
        self._total_short_bypasses = 0
        self.lock_file = None
        self.prefix = 'clawbox-' + uuid.uuid4().hex
        self.high = self.parent / (self.prefix + '-high')
        self.low = self.parent / (self.prefix + '-low')
        self.paths: list[Path] = []
        self.leaves: dict[tuple[int, str], Path] = {}
        self.priorities = {i: session_priority(i, concurrency, repetition) if legacy else 'neutral'
                           for i in range(concurrency)}
        # Equal sibling defaults would accidentally give one HIGH session half
        # of the CPU shares. Normalize by offered sessions in every arm,
        # including OFF. These are shares, not reservations or measured use.
        self.cpu_weights = {'high': max(1, round(10000/concurrency)),
                            'low': max(1, 10000-round(10000/concurrency))}
        self.tokens = {(i, role): uuid.uuid4().hex for i in self.priorities for role in ('runtime','tool')}
        self.journal = journal_directory / (self.prefix+'.json') if journal_directory else None
        if topology_mode not in {'c0', 'c2'}:
            raise ValueError(f'unknown topology CPU placement mode: {topology_mode}')
        if topology_pressure_samples < 1:
            raise ValueError('topology pressure samples must be positive')
        if topology_group_max_active < 1:
            raise ValueError('topology group maximum active assignments must be positive')
        if topology_min_duration_seconds < 0:
            raise ValueError('topology minimum call duration must not be negative')
        for index, (cpus, node) in sorted(self.placements.items()):
            self.configure_topology_placement(index, cpus, node)

    @contextmanager
    def _topology_io(self):
        """Keep same-VM ordering while releasing cross-VM accounting for I/O."""
        owned = self._topology_lock._is_owned()
        state = self._topology_lock._release_save() if owned else None
        try:
            yield
        finally:
            if owned:
                self._topology_lock._acquire_restore(state)

    def _record_topology_event(self, event):
        self.topology_events.append(event)
        with self._topology_io():
            self.emit(event)

    def _sample_group_utilization(self, groups) -> dict[str, float | None]:
        from .topology import parse_cpu_list
        with self._topology_io():
            try:
                current = _cpu_times(self._proc_stat)
            except OSError:
                current = {}
            captured = time.monotonic()
        if captured < self._cpu_sample_at:
            return {group: self._latest_group_utilization.get(group) for group in groups}
        self._cpu_sample_at = captured
        utilization = {}
        for group in groups:
            busy_delta = total_delta = 0
            for cpu in parse_cpu_list(group):
                if cpu not in current or cpu not in self._previous_cpu_times:
                    continue
                busy_delta += current[cpu][0] - self._previous_cpu_times[cpu][0]
                total_delta += current[cpu][1] - self._previous_cpu_times[cpu][1]
            utilization[group] = (max(0., min(1., busy_delta/total_delta))
                                  if total_delta > 0 else None)
        self._previous_cpu_times = current
        self._latest_group_utilization.update(utilization)
        return utilization

    def configure_topology_placement(self, index: int, cpus: str | None, node: int | None):
        """Record the existing NUMA placement without changing C0 CPU behavior."""
        with self._topology_lock:
            self._topology_sessions.setdefault(index, RLock())
            existing = self.placements.get(index)
            if existing is not None and existing != (cpus, node):
                raise RuntimeError(f'CPU placement changed for session {index}: {existing} -> {(cpus, node)}')
            self.placements[index] = cpus, node
            if self.topology_mode == 'c2' and cpus is None:
                raise RuntimeError('C2 Tool-call placement requires an explicit compute CPU pool')

    def acquire_tool_call(self, index: int, execution_id: str, *, vcpus: int) -> dict | None:
        """Register one eligible exec and apply C2 after its configured delay.

        Concurrent calls in the same Tool VM share its active assignment.  The
        assignment is released, and the full C0 mask restored, after the final
        active call completes.
        """
        if self.topology_mode == 'c0':
            return None
        with timed_lock(self._topology_sessions[index], 'cpu.session'):
            return self._acquire_tool_call(index, execution_id, vcpus=vcpus)

    def _acquire_tool_call(self, index: int, execution_id: str, *, vcpus: int):
        if execution_id in self._call_keys:
            raise RuntimeError(f'duplicate Tool-call CPU assignment: {execution_id}')
        key = index, 'tool'
        with timed_lock(self._topology_lock, 'cpu.registry'):
            if self._topology_stopping:
                raise RuntimeError('Tool-call CPU placement is stopping for VM teardown')
            active = self._active_calls.get(key)
            if active:
                active.add(execution_id)
                self._call_keys[execution_id] = key
                pending = key in self._pending_started
                full_numa = key in self._capacity_bypassed
                event = {
                    'event': 'tool_call_cpu_placement',
                    'action': ('share_pending' if pending else
                               'share_full_numa' if full_numa else 'share'),
                    'execution_id': execution_id, 'session_index': index,
                    'selected_cpus': self._fine_masks.get(key),
                    'active_calls': len(active), 'mode': self.topology_mode,
                }
                self._record_topology_event(event)
                return event
            if self.topology_min_duration_seconds > 0:
                self._active_calls[key] = {execution_id}
                self._call_keys[execution_id] = key
                self._pending_started[key] = time.monotonic()
                self._pending_vcpus[key] = vcpus
                cpus, node = self.placements.get(index, (None, None))
                if cpus is None:
                    raise RuntimeError(f'C2 has no selected-NUMA CPU mask for session {index}')
                event = {
                    'event': 'tool_call_cpu_placement', 'action': 'defer',
                    'execution_id': execution_id, 'session_index': index,
                    'compute_numa': node, 'full_cpus': cpus,
                    'minimum_duration_seconds': self.topology_min_duration_seconds,
                    'release_required': True, 'mode': self.topology_mode,
                }
                self._record_topology_event(event)
                return event
            return self._assign_tool_call(index, execution_id, vcpus=vcpus)

    def _assign_tool_call(self, index: int, execution_id: str, *, vcpus: int) -> dict:
        """Narrow one new call after any minimum-duration delay has elapsed."""
        key = index, 'tool'
        cpus, node = self.placements.get(index, (None, None))
        if cpus is None:
            raise RuntimeError(f'C2 has no selected-NUMA CPU mask for session {index}')
        pool_key = cpus, vcpus, self.topology_include_smt
        groups = self._groups_by_pool.get(pool_key)
        if groups is None:
            if self.topology_include_smt:
                groups = physical_cluster_groups(
                    cpus, min_physical_cores=vcpus, include_smt=True,
                )
            else:
                groups = physical_cluster_groups(cpus, min_physical_cores=vcpus)
            self._groups_by_pool[pool_key] = groups
        if len(groups) < 2:
            raise RuntimeError(
                f'C2 requires at least two LLC/cluster physical-core groups in CPUs {cpus}'
            )
        self._candidate_groups.update(groups)
        measured_load = self._sample_group_utilization(groups)
        available = tuple(
            group for group in groups
            if self._group_load.get(group, 0) < self.topology_group_max_active
        )
        if not available:
            self._total_capacity_bypasses += 1
            event = {
                'event': 'tool_call_cpu_placement',
                'action': 'bypass_full_numa',
                'execution_id': execution_id,
                'session_index': index,
                'compute_numa': node,
                'full_cpus': cpus,
                'reason': 'group_capacity',
                'group_max_active': self.topology_group_max_active,
                'group_active_assignments': {
                    group: self._group_load.get(group, 0) for group in groups
                },
                'candidate_group_cpu_utilization': measured_load,
                'release_required': False,
                'mode': self.topology_mode,
            }
            self._record_topology_event(event)
            return event
        preferred = self._preferred_groups.get(key)
        group = min(available, key=lambda item: (
            self._group_load.get(item, 0),
            item != preferred,
            measured_load.get(item) is None,
            measured_load.get(item) or 0., item,
        ))
        # Reserve group capacity atomically before releasing the registry for
        # affinity I/O. Other VMs can choose groups without overcommitting it.
        self._group_load[group] = self._group_load.get(group, 0) + 1
        # Do not publish assignment ownership until every extant vCPU
        # thread has accepted and verified the narrow mask.  A failed
        # multi-thread update may already have changed earlier threads, so
        # restore the C0 mask before propagating the policy failure.
        try:
            applied = self._apply_thread_affinity(index, 'tool', group)
        except BaseException:
            try:
                self._apply_thread_affinity(index, 'tool', cpus, strict=False)
            except BaseException:
                pass
            self._group_load[group] -= 1
            raise
        self._preferred_groups[key] = group
        self._full_masks[key] = cpus
        self._fine_masks[key] = group
        self._active_calls[key] = {execution_id}
        self._call_keys[execution_id] = key
        self._expanded.discard(key)
        self._pressure_streak.pop(key, None)
        try:
            with self._topology_io():
                initial_pressure = _pressure_total(self.leaves[key]/'cpu.pressure')
            self._pressure_previous[key] = time.monotonic(), initial_pressure
        except (OSError, RuntimeError, ValueError):
            self._pressure_previous.pop(key, None)
        self._total_call_assignments += 1
        event = {
            'event': 'tool_call_cpu_placement', 'action': 'assign',
            'execution_id': execution_id, 'session_index': index,
            'compute_numa': node, 'selected_cpus': group, 'full_cpus': cpus,
            'topology_level': ('complete_llc_cluster_logical_cpus'
                               if self.topology_include_smt
                               else 'complete_llc_cluster_physical_cores'),
            'candidate_group_cpu_utilization': measured_load,
            'group_active_assignments': self._group_load[group],
            'group_max_active': self.topology_group_max_active,
            'preferred_group_reused': group == preferred,
            'threads': applied, 'mode': self.topology_mode,
        }
        self._record_topology_event(event)
        return event

    def active_tool_call_ids(self) -> tuple[str, ...]:
        with self._topology_lock:
            return tuple(sorted(self._call_keys))

    def begin_teardown(self, *, cancel_calls: bool = False) -> None:
        """Quiesce C2 before destroying VMs, retaining explicit cancellation evidence."""
        with self._topology_lock:
            self._topology_stopping = True
            session_locks = tuple(self._topology_sessions.values())
        # Existing same-VM operations may be outside the registry doing I/O.
        # Drain them before clearing ownership and before the caller destroys VMs.
        with ExitStack() as guards:
            for lock in session_locks:
                guards.enter_context(lock)
            self._finish_topology_teardown(cancel_calls)

    def _finish_topology_teardown(self, cancel_calls):
        with self._topology_lock:
            if cancel_calls and self._call_keys:
                event = {
                    'event': 'tool_call_cpu_placement', 'action': 'cancel_for_teardown',
                    'execution_ids': sorted(self._call_keys), 'mode': self.topology_mode,
                }
                self.topology_events.append(event); self.emit(event)
                self._call_keys.clear()
                self._active_calls.clear()
                self._pending_started.clear()
                self._pending_vcpus.clear()
                self._capacity_bypassed.clear()
                self._full_masks.clear()
                self._fine_masks.clear()
                self._group_load.clear()
                self._expanded.clear()
                self._pressure_previous.clear()
                self._pressure_streak.clear()

    def release_tool_call(self, execution_id: str) -> dict | None:
        if self.topology_mode == 'c0':
            return None
        # Lookup only selects a stable session lock. Ownership is rechecked
        # under that lock and the registry before changing any assignment.
        key = self._call_keys.get(execution_id)
        if key is None:
            if self._topology_stopping:
                return None
            raise RuntimeError(f'no Tool-call CPU assignment for completion: {execution_id}')
        with timed_lock(self._topology_sessions[key[0]], 'cpu.session'):
            return self._release_tool_call(execution_id)

    def _release_tool_call(self, execution_id):
        with timed_lock(self._topology_progress_lock, 'cpu.placement'):
            key = self._call_keys.get(execution_id)
            if key is None:
                if self._topology_stopping:
                    return None
                raise RuntimeError(f'no Tool-call CPU assignment for completion: {execution_id}')
            active = self._active_calls[key]
            if len(active) > 1:
                active.remove(execution_id)
                self._call_keys.pop(execution_id)
                pending = key in self._pending_started
                full_numa = key in self._capacity_bypassed
                event = {
                    'event': 'tool_call_cpu_placement',
                    'action': ('unshare_pending' if pending else
                               'unshare_full_numa' if full_numa else 'unshare'),
                    'execution_id': execution_id, 'session_index': key[0],
                    'selected_cpus': self._fine_masks.get(key),
                    'active_calls': len(active), 'mode': self.topology_mode,
                }
                self._record_topology_event(event)
                return event
            if key not in self._fine_masks:
                pending = key in self._pending_started
                active.remove(execution_id)
                self._call_keys.pop(execution_id)
                self._active_calls.pop(key)
                self._pending_started.pop(key, None)
                self._pending_vcpus.pop(key, None)
                capacity_bypassed = key in self._capacity_bypassed
                self._capacity_bypassed.discard(key)
                if pending:
                    self._total_short_bypasses += 1
                event = {
                    'event': 'tool_call_cpu_placement',
                    'action': 'complete_full_numa',
                    'execution_id': execution_id, 'session_index': key[0],
                    'reason': 'short_call' if pending else 'group_capacity',
                    'capacity_bypassed': capacity_bypassed,
                    'mode': self.topology_mode,
                }
                self._record_topology_event(event)
                return event
            applied = self._apply_thread_affinity(key[0], 'tool', self._full_masks[key])
            active.remove(execution_id)
            self._call_keys.pop(execution_id)
            fine = self._fine_masks.pop(key)
            self._group_load[fine] -= 1
            self._active_calls.pop(key)
            self._expanded.discard(key)
            self._pressure_previous.pop(key, None)
            self._pressure_streak.pop(key, None)
            event = {
                'event': 'tool_call_cpu_placement', 'action': 'restore_c0',
                'execution_id': execution_id, 'session_index': key[0],
                'from_cpus': fine, 'to_cpus': self._full_masks.pop(key),
                'threads': applied, 'mode': self.topology_mode,
            }
            self._record_topology_event(event)
            return event

    @operation('cpu.affinity')
    def _apply_thread_affinity(self, index: int, role: str, cpus: str, *, strict: bool = True) -> list[dict]:
        with self._topology_io():
            return self._apply_thread_affinity_io(index, role, cpus, strict=strict)

    def _apply_thread_affinity_io(self, index, role, cpus, *, strict):
        from .topology import parse_cpu_list
        leaf = self.leaves[index, role]
        requested = parse_cpu_list(cpus)
        key = index, role
        cached = self._vcpu_threads.get(key)
        if cached is None:
            discovered = []
            for tid_text in (leaf/'cgroup.threads').read_text().split():
                tid = int(tid_text)
                try:
                    name = next(line.split(':', 1)[1].strip()
                                for line in Path(f'/proc/{tid}/status').read_text().splitlines()
                                if line.startswith('Name:'))
                except (FileNotFoundError, ProcessLookupError, StopIteration):
                    continue
                if name.startswith('vcpu'):
                    discovered.append((tid, name))
            cached = tuple(discovered)
            if cached:
                self._vcpu_threads[key] = cached
        applied = []
        stale = False
        for tid, name in cached:
            try:
                os.sched_setaffinity(tid, requested)
                effective = set(os.sched_getaffinity(tid))
            except ProcessLookupError:
                stale = True
                continue
            if effective != requested:
                raise RuntimeError(
                    f'vCPU affinity mismatch for {tid}: requested {cpus}, got {_format_cpu_list(effective)}'
                )
            applied.append({'tid': tid, 'name': name, 'cpus_allowed': _format_cpu_list(effective)})
        if stale:
            self._vcpu_threads.pop(key, None)
        if not applied and strict:
            raise RuntimeError(f'no vCPU threads available for topology placement: {leaf}')
        return applied

    def sample_topology_placement(self) -> list[dict]:
        if self.topology_mode == 'c0':
            return []
        now = time.monotonic()
        changes = []
        with self._topology_lock:
            if self._topology_stopping:
                return []
            pending = tuple(self._pending_started.items())
        # One VM per lock handoff: completions must not queue behind a scan
        # and affinity updates for every active VM in the experiment.
        for key, started in pending:
            with timed_lock(self._topology_sessions[key[0]], 'cpu.session'), \
                    timed_lock(self._topology_lock, 'cpu.poll_vm'):
                if self._topology_stopping:
                    return changes
                if self._pending_started.get(key) != started:
                    continue
                if now-started < self.topology_min_duration_seconds:
                    continue
                active = self._active_calls[key]
                primary = min(active)
                vcpus = self._pending_vcpus.pop(key)
                self._pending_started.pop(key, None)
                event = self._assign_tool_call(key[0], primary, vcpus=vcpus)
                if not event.get('release_required', True):
                    self._active_calls[key] = active
                    for execution_id in active:
                        self._call_keys[execution_id] = key
                    self._capacity_bypassed.add(key)
                else:
                    self._active_calls[key] = active
                    for execution_id in active:
                        self._call_keys[execution_id] = key
                changes.append(event)
        with self._topology_lock:
            if self._topology_stopping:
                return changes
            measured_load = self._sample_group_utilization(sorted(self._candidate_groups))
            if measured_load:
                self._record_topology_event({
                    'event': 'topology_cpu_group_load',
                    'mode': self.topology_mode,
                    'candidate_group_cpu_utilization': measured_load,
                })
            assigned = tuple((key, fine, self._active_calls.get(key))
                             for key, fine in self._fine_masks.items())
        for key, fine, active in assigned:
            with timed_lock(self._topology_sessions[key[0]], 'cpu.session'), \
                    timed_lock(self._topology_lock, 'cpu.poll_vm'):
                if self._topology_stopping:
                    return changes
                if (self._active_calls.get(key) is not active
                        or self._fine_masks.get(key) != fine):
                    continue
                if key in self._expanded:
                    continue
                index, role = key
                leaf = self.leaves.get(key)
                try:
                    with self._topology_io():
                        populated = leaf is not None and bool((leaf/'cgroup.procs').read_text().strip())
                except FileNotFoundError:
                    continue
                if not populated:
                    continue
                try:
                    with self._topology_io():
                        total = _pressure_total(leaf/'cpu.pressure')
                except (OSError, RuntimeError, ValueError) as exc:
                    applied = self._apply_thread_affinity(
                        index, role, self._full_masks[key], strict=False,
                    )
                    if not applied:
                        continue
                    self._expanded.add(key)
                    event = {
                        'event': 'tool_call_cpu_placement', 'action': 'expand',
                        'session_index': index, 'role': role, 'mode': self.topology_mode,
                        'from_cpus': fine, 'to_cpus': self._full_masks[key],
                        'reason': 'pressure_unavailable', 'detail': str(exc),
                        'selected_group_cpu_utilization': measured_load.get(fine),
                        'threads': applied,
                    }
                    self._record_topology_event(event); changes.append(event)
                    self._total_expansions += 1
                    continue
                previous = self._pressure_previous.get(key)
                self._pressure_previous[key] = now, total
                if previous is None or now <= previous[0] or total < previous[1]:
                    continue
                ratio = (total-previous[1]) / ((now-previous[0])*1_000_000)
                streak = self._pressure_streak.get(key, 0) + 1 if ratio >= self.topology_pressure_ratio else 0
                self._pressure_streak[key] = streak
                if streak < self.topology_pressure_samples:
                    continue
                applied = self._apply_thread_affinity(
                    index, role, self._full_masks[key], strict=False,
                )
                if not applied:
                    continue
                self._expanded.add(key)
                event = {
                    'event': 'tool_call_cpu_placement', 'action': 'expand',
                    'session_index': index, 'role': role, 'mode': self.topology_mode,
                    'from_cpus': fine, 'to_cpus': self._full_masks[key],
                    'reason': 'cpu_pressure', 'pressure_ratio': ratio,
                    'threshold': self.topology_pressure_ratio, 'threads': applied,
                    'selected_group_cpu_utilization': measured_load.get(fine),
                }
                self._record_topology_event(event); changes.append(event)
                self._total_expansions += 1
        return changes

    def topology_summary(self) -> dict:
        return {
            'mode': self.topology_mode,
            'scope': 'agent_tool_exec',
            'pressure_ratio': self.topology_pressure_ratio,
            'pressure_samples': self.topology_pressure_samples,
            'group_max_active': self.topology_group_max_active,
            'minimum_duration_seconds': self.topology_min_duration_seconds,
            'include_smt': self.topology_include_smt,
            'assignments': self._total_call_assignments,
            'capacity_bypasses': self._total_capacity_bypasses,
            'short_call_bypasses': self._total_short_bypasses,
            'active_assignments': len(self._fine_masks),
            'total_expansions': self._total_expansions,
            'active_expanded': len(self._expanded),
            'latest_candidate_group_cpu_utilization': dict(self._latest_group_utilization),
            'events': list(self.topology_events),
        }

    def _journal(self):
        if self.journal is None:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open('x', encoding='utf-8') as stream:
            os.chmod(self.journal, 0o600)
            json.dump({'parent':str(self.parent),'prefix':self.prefix,
                       'priorities':self.priorities,'tokens':list(self.tokens.values()),
                       'native': not self.legacy},stream)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _enable(path: Path):
        controllers = (path / 'cgroup.controllers').read_text().split()
        required = {'memory', 'cpu', 'cpuset'}
        if not required.issubset(controllers):
            raise RuntimeError(f'missing cgroup controllers at {path}: {required-set(controllers)}')
        (path / 'cgroup.subtree_control').write_text('+memory +cpu +cpuset')

    def _create(self, path: Path, *, branch: bool, high: bool = False):
        path.mkdir()
        self.paths.append(path)
        # Explicit descendant protection also works without memory_recursiveprot.
        # The HIGH ancestor's changing memory.low remains the effective budget.
        if high:
            (path / 'memory.low').write_text('max')
        if branch:
            self._enable(path)

    def start(self):
        if os.geteuid() != 0:
            raise RuntimeError('priority domain registration requires a root experiment worker')
        if not self.parent.is_relative_to(self.cgroup_mount/'cube_sandbox'/'sandbox'):
            raise ValueError('priority domain must be inside the Cube VM memory subtree')
        self.registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.registry.mkdir(exist_ok=True, mode=0o700)
        for directory in (self.registry.parent, self.registry):
            st = directory.lstat()
            if directory.is_symlink() or st.st_uid != 0 or st.st_mode & 0o077:
                raise RuntimeError('unsafe Cube cgroup registry ownership or permissions')
        self._enable(self.parent)
        try:
            import fcntl
            name = hashlib.sha256(str(self.parent).encode()).hexdigest() + '.lock'
            self.lock_file = (self.registry / name).open('a')
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._journal()
            if not self.legacy:
                self.cpu_weights = {'default': 100}
                return
            events = dict(
                line.split()
                for line in (self.parent/'cgroup.events').read_text().splitlines()
            )
            if events.get('populated') != '0':
                raise RuntimeError('physical VM domain is not empty before the arm')
            self._create(self.high, branch=True)
            self._create(self.low, branch=True)
            (self.high/'cpu.weight').write_text(str(self.cpu_weights['high']))
            (self.low/'cpu.weight').write_text(str(self.cpu_weights['low']))
            for index, priority in self.priorities.items():
                session = (self.high if priority == 'high' else self.low) / f'session-{index:04d}'
                self._create(session, branch=True, high=priority == 'high')
                for role in ('runtime', 'tool'):
                    leaf = session / role
                    self._create(leaf, branch=False, high=priority == 'high')
                    token = self.tokens[index, role]
                    self.leaves[index, role] = leaf
                    self.prepare(index, role)
                    with (self.registry / token).open('x', encoding='utf-8') as stream:
                        os.chmod(stream.name, 0o600)
                        json.dump({'path': '/' + leaf.relative_to('/sys/fs/cgroup').as_posix()}, stream)
        except BaseException:
            self.close()
            raise

    def token(self, index: int, role: str) -> str:
        return self.tokens[index, role]

    def prepare(self, index: int, role: str):
        if not self.legacy:
            self._vcpu_threads.pop((index, role), None)
            self.leaves.pop((index, role), None)
            _cpus, node = self.placements.get(index, (None, None))
            if node is None:
                raise RuntimeError(
                    f'CubeSandbox native placement has no NUMA node for session {index}'
                )
            token = self.tokens[index, role]
            target = self.registry / token
            temporary = self.registry / f'.{token}.{os.getpid()}'
            temporary.unlink(missing_ok=True)
            try:
                with temporary.open('x', encoding='utf-8') as stream:
                    os.chmod(stream.name, 0o600)
                    json.dump({'numa_node': node}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return
        leaf = self.leaves[index, role]
        self._vcpu_threads.pop((index, role), None)
        # Cube's pause/destroy response can precede the final VMM thread leaving
        # its preallocated leaf. Restore may immediately reuse that same leaf.
        deadline = time.monotonic() + self.leaf_release_grace_seconds
        while (leaf/'cgroup.procs').read_text().strip() and time.monotonic() < deadline:
            time.sleep(.05)
        # A failed Cube resume can leave its old shim alive indefinitely even
        # though the API operation has ended.  prepare() runs before Cube is
        # asked to create the replacement VM, so every remaining process is a
        # stale owner of this exact preallocated leaf.  Use the kernel's narrow
        # cgroup kill operation rather than scanning or killing unrelated PIDs.
        if (leaf/'cgroup.procs').read_text().strip():
            kill = leaf/'cgroup.kill'
            if kill.exists():
                kill.write_text('1')
                deadline = time.monotonic() + self.leaf_kill_grace_seconds
                while ((leaf/'cgroup.procs').read_text().strip()
                       and time.monotonic() < deadline):
                    time.sleep(.05)
        if (leaf/'cgroup.procs').read_text().strip():
            raise RuntimeError(f'priority VM leaf is already populated: {leaf}')
        cpus, node = self.placements.get(index, (None, None))
        # Even an unchanged cpuset write flushes the kernel's migration work
        # queue. Preallocated LOCAL leaves need no rewrite on ordinary create;
        # a borrowed/restored leaf must still be reset when its value differs.
        if cpus is not None and (leaf/'cpuset.cpus').read_text().strip() != cpus:
            (leaf/'cpuset.cpus').write_text(cpus)
        if node is not None and (leaf/'cpuset.mems').read_text().strip() != str(node):
            (leaf/'cpuset.mems').write_text(str(node))

    def verify(self, index: int, role: str, sandbox_id: str) -> dict:
        if not self.legacy:
            return self._verify_native(index, role, sandbox_id)
        from .topology import parse_cpu_list
        from .numa_borrow import read_numa_lru_bytes
        expected = self.leaves[index, role]
        # The leaf was empty immediately before Cube received the opaque token,
        # and Cubelet can resolve that token only to this root-owned registry
        # entry.  A populated leaf with vCPU threads therefore proves routing
        # without an O(number of host processes) /proc scan for every VM.
        actual = expected.resolve()
        if actual == self.parent or not actual.is_relative_to(self.parent):
            raise RuntimeError(f'Cube preallocation cgroup escaped its domain: {actual}')
        cpus = (actual/'cpuset.cpus.effective').read_text().strip()
        mems = (actual/'cpuset.mems.effective').read_text().strip()
        configured_cpus, configured_node = self.placements.get(index, (None, None))
        required_cpus = configured_cpus or (self.parent/'cpuset.cpus.effective').read_text().strip()
        required_mems = (str(configured_node) if configured_node is not None else
                         (self.parent/'cpuset.mems.effective').read_text().strip())
        if parse_cpu_list(cpus) != parse_cpu_list(required_cpus) or parse_cpu_list(mems) != parse_cpu_list(required_mems):
            raise RuntimeError(f'VM effective cpuset mismatch at {actual}: CPUs={cpus}, memory={mems}')
        topology_threads = None
        key = index, role
        if key in self._fine_masks:
            selected = self._full_masks[key] if key in self._expanded else self._fine_masks[key]
            topology_threads = self._apply_thread_affinity(index, role, selected)
            try:
                self._pressure_previous[key] = time.monotonic(), _pressure_total(actual/'cpu.pressure')
            except (OSError, RuntimeError, ValueError):
                pass
        threads = []
        for tid in (actual/'cgroup.threads').read_text().split():
            try:
                status = dict(line.split(':', 1) for line in Path(f'/proc/{tid}/status').read_text().splitlines() if ':' in line)
            except (FileNotFoundError, ProcessLookupError):
                continue  # auxiliary I/O threads may exit during observation
            allowed = status['Cpus_allowed_list'].strip()
            if not parse_cpu_list(allowed) <= parse_cpu_list(cpus):
                raise RuntimeError(f'VM thread {tid} escaped compute cpuset')
            threads.append({'tid':int(tid), 'name':status['Name'].strip(), 'cpus_allowed':allowed})
        if not any(t['name'].startswith('vcpu') for t in threads):
            raise RuntimeError(f'Cube preallocation cgroup routing failed: no vCPU threads in {actual}')
        self._vcpu_threads[key] = tuple(
            (thread['tid'], thread['name']) for thread in threads
            if thread['name'].startswith('vcpu')
        )
        resident, unattributed = read_numa_lru_bytes(actual)
        evidence = {'priority': self.priorities[index], 'cgroup': str(actual),
                'sandbox_id': sandbox_id, 'role': role, 'compute_numa':configured_node,
                'effective_cpus':cpus, 'memory_nodes':sorted(parse_cpu_list(mems)),
                'actual_residency_bytes':resident, 'unattributed_bytes':unattributed,
                'threads':threads, 'enforcement':'preallocation_cgroup',
                'tool_call_cpu_placement': ({
                    'mode': self.topology_mode,
                    'selected_cpus': self._full_masks[key] if key in self._expanded else self._fine_masks[key],
                    'full_cpus': self._full_masks[key], 'target_threads': topology_threads,
                } if topology_threads is not None else None)}
        self.observations.append(evidence)
        return evidence

    def _verify_native(self, index: int, role: str, sandbox_id: str) -> dict:
        from .topology import parse_cpu_list
        from .numa_borrow import read_numa_anon_bytes, read_numa_lru_bytes

        token = self.tokens[index, role]
        record = json.loads((self.registry/token).read_text(encoding='utf-8'))
        relative = record.get('path')
        _configured_cpus, configured_node = self.placements.get(index, (None, None))
        if (record.get('numa_node') != configured_node
                or not isinstance(relative, str)
                or OFFICIAL_CGROUP.fullmatch(relative) is None):
            raise RuntimeError(
                f'CubeSandbox did not publish a valid native VM cgroup for {sandbox_id}'
            )
        actual = (self.cgroup_mount/relative.lstrip('/')).resolve()
        if actual.parent != self.parent:
            raise RuntimeError(f'CubeSandbox VM cgroup escaped its official pool: {actual}')
        key = index, role
        self.leaves[key] = actual
        cpus = (actual/'cpuset.cpus.effective').read_text().strip()
        mems = (actual/'cpuset.mems.effective').read_text().strip()
        configured_cpus, configured_node = self.placements[index]
        required_cpus = (
            configured_cpus
            or Path(f'/sys/devices/system/node/node{configured_node}/cpulist').read_text().strip()
        )
        if not parse_cpu_list(required_cpus) <= parse_cpu_list(cpus):
            raise RuntimeError(
                f'CubeSandbox native cgroup excludes requested CPUs at {actual}: '
                f'requested={required_cpus}, effective={cpus}'
            )
        threads = []
        for tid_text in (actual/'cgroup.threads').read_text().split():
            tid = int(tid_text)
            try:
                status = dict(
                    line.split(':', 1)
                    for line in Path(f'/proc/{tid}/status').read_text().splitlines()
                    if ':' in line
                )
            except (FileNotFoundError, ProcessLookupError):
                continue
            threads.append({
                'tid': tid,
                'name': status['Name'].strip(),
                'cpus_allowed': status['Cpus_allowed_list'].strip(),
            })
        vcpus = [thread for thread in threads if thread['name'].startswith('vcpu')]
        if not vcpus:
            raise RuntimeError(f'CubeSandbox native cgroup has no vCPU threads: {actual}')
        requested = parse_cpu_list(required_cpus)
        escaped = [
            thread for thread in vcpus
            if not parse_cpu_list(thread['cpus_allowed']) <= requested
        ]
        if escaped:
            raise RuntimeError(
                f'CubeSandbox native vCPU affinity escaped NUMA CPUs {required_cpus}: {escaped}'
            )
        self._vcpu_threads[key] = tuple(
            (thread['tid'], thread['name']) for thread in vcpus
        )
        resident, unattributed = read_numa_lru_bytes(actual)
        anon_resident = read_numa_anon_bytes(actual)
        attributed_anon = sum(anon_resident.values())
        initial_residency_check = sandbox_id not in self._initial_residency_verified
        # Incremental restore can leave all guest RAM file-backed until the
        # guest dirties it. Shared template/snapshot cache is not controlled by
        # the VMM allocation policy, so validate anonymous pages when present
        # and record an unobservable sample when none exist.
        local_anon = anon_resident.get(configured_node, 0)
        if (initial_residency_check
                and attributed_anon >= PLACEMENT_SAMPLE_MIN_BYTES
                and local_anon * 2 <= attributed_anon):
            raise RuntimeError(
                f'CubeSandbox native memory placement is not local to NUMA node '
                f'{configured_node}: anon_residency={anon_resident}, '
                f'total_residency={resident}, unattributed={unattributed}'
            )
        self._initial_residency_verified.add(sandbox_id)
        topology_threads = None
        if key in self._fine_masks:
            selected = self._full_masks[key] if key in self._expanded else self._fine_masks[key]
            topology_threads = self._apply_thread_affinity(index, role, selected)
            try:
                self._pressure_previous[key] = (
                    time.monotonic(), _pressure_total(actual/'cpu.pressure')
                )
            except (OSError, RuntimeError, ValueError):
                pass
        evidence = {
            'priority': self.priorities[index],
            'cgroup': str(actual),
            'sandbox_id': sandbox_id,
            'role': role,
            'compute_numa': configured_node,
            'effective_cpus': cpus,
            'memory_nodes': sorted(parse_cpu_list(mems)),
            'requested_cpus': required_cpus,
            'actual_residency_bytes': resident,
            'actual_anon_residency_bytes': anon_resident,
            'unattributed_bytes': unattributed,
            'residency_basis': (
                'anonymous_initial_create'
                if initial_residency_check and attributed_anon >= PLACEMENT_SAMPLE_MIN_BYTES
                else 'insufficient_anonymous_sample_initial_create'
                if initial_residency_check
                else 'restore_policy_only'
            ),
            'local_residency_fraction': (
                local_anon / attributed_anon
                if (initial_residency_check
                    and attributed_anon >= PLACEMENT_SAMPLE_MIN_BYTES) else None
            ),
            'threads': threads,
            'enforcement': 'cubesandbox_native_mempolicy_and_affinity',
            'tool_call_cpu_placement': ({
                'mode': self.topology_mode,
                'selected_cpus': (
                    self._full_masks[key] if key in self._expanded else self._fine_masks[key]
                ),
                'full_cpus': self._full_masks[key],
                'target_threads': topology_threads,
            } if topology_threads is not None else None),
        }
        self.observations.append(evidence)
        return evidence

    def close(self):
        # Revoke capabilities first; never recursively delete an arbitrary path.
        for token in self.tokens.values():
            (self.registry / token).unlink(missing_ok=True)
        if not self.legacy:
            if self.lock_file is not None:
                self.lock_file.close()
                self.lock_file = None
            if self.journal is not None:
                self.journal.unlink(missing_ok=True)
            return
        # Cube's destroy response can precede the final VMM thread leaving its
        # cgroup by a few seconds.  Retry the same known leaves rather than
        # making the next qualification or arm fail on that teardown race.
        pending = list(reversed(self.paths))
        deadline = time.monotonic() + 15
        last_errors: dict[Path, OSError] = {}
        while pending:
            retry = []
            for path in pending:
                try:
                    path.rmdir()
                    last_errors.pop(path, None)
                except FileNotFoundError:
                    last_errors.pop(path, None)
                except OSError as exc:
                    retry.append(path)
                    last_errors[path] = exc
            if not retry or time.monotonic() >= deadline:
                pending = retry
                break
            pending = retry
            time.sleep(0.1)
        # Cube may lose the API record while an owned shim remains alive.  At
        # this point every sandbox has already been destroyed and capabilities
        # are revoked, so terminate processes only inside our exact leaves.
        if pending:
            for path in pending:
                kill = path / 'cgroup.kill'
                try:
                    if kill.exists():
                        kill.write_text('1')
                except FileNotFoundError:
                    pass
            deadline = time.monotonic() + 5
            while pending:
                retry = []
                for path in pending:
                    try:
                        path.rmdir()
                        last_errors.pop(path, None)
                    except FileNotFoundError:
                        last_errors.pop(path, None)
                    except OSError as exc:
                        retry.append(path)
                        last_errors[path] = exc
                if not retry or time.monotonic() >= deadline:
                    pending = retry
                    break
                pending = retry
                time.sleep(0.1)
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None
        if pending:
            raise RuntimeError(
                'priority domain cleanup failed: '
                + '; '.join(str(last_errors[path]) for path in pending)
            )
        if self.journal is not None:
            self.journal.unlink(missing_ok=True)


def cleanup_priority_domains(directory: Path):
    """Supervisor recovery after a worker is forcibly killed; VMs must die first."""
    import fcntl
    REGISTRY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    REGISTRY.mkdir(exist_ok=True, mode=0o700)
    for folder in (REGISTRY.parent, REGISTRY):
        st=folder.lstat()
        if folder.is_symlink() or st.st_uid!=0 or st.st_mode&0o077:
            raise RuntimeError('unsafe priority cleanup registry')
    for journal in directory.glob('clawbox-*.json'):
        st=journal.lstat()
        if journal.is_symlink() or st.st_uid != 0 or st.st_mode & 0o077:
            raise RuntimeError('unsafe priority cleanup journal')
        record=json.loads(journal.read_text())
        prefix=record['prefix']; parent=Path(record['parent']).resolve()
        if (not re.fullmatch(r'clawbox-[0-9a-f]{32}',prefix)
                or journal.name != prefix+'.json'
                or not parent.is_relative_to(Path('/sys/fs/cgroup/cube_sandbox/sandbox'))):
            raise ValueError('invalid priority cleanup domain')
        name=hashlib.sha256(str(parent).encode()).hexdigest()+'.lock'
        with (REGISTRY/name).open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for token in record['tokens']:
                if not re.fullmatch(r'[0-9a-f]{32}',token):
                    raise ValueError('invalid priority cleanup token')
                (REGISTRY/token).unlink(missing_ok=True)
            if record.get('native'):
                journal.unlink()
                continue
            paths=[]
            for index,priority in record['priorities'].items():
                if not index.isdecimal() or priority not in ('high','low','neutral'):
                    raise ValueError('invalid priority cleanup session')
                session=parent/(prefix+'-'+priority)/f'session-{int(index):04d}'
                paths.extend([session/'runtime',session/'tool',session])
            paths.extend([parent/(prefix+'-high'),parent/(prefix+'-low')])
            # A killed worker cannot run PriorityDomain.close().  Cube's API
            # can still report a sandbox destroyed before its last shim leaves
            # the owned leaf, so recovery needs the same bounded wait and
            # exact-cgroup kill used by normal teardown.
            pending=list(paths)
            deadline=time.monotonic()+15
            last_errors: dict[Path,OSError]={}
            while pending:
                retry=[]
                for path in pending:
                    try:
                        path.rmdir()
                        last_errors.pop(path,None)
                    except FileNotFoundError:
                        last_errors.pop(path,None)
                    except OSError as exc:
                        retry.append(path); last_errors[path]=exc
                if not retry or time.monotonic()>=deadline:
                    pending=retry; break
                pending=retry; time.sleep(.1)
            if pending:
                for path in pending:
                    kill=path/'cgroup.kill'
                    try:
                        if kill.exists():
                            kill.write_text('1')
                    except FileNotFoundError:
                        pass
                deadline=time.monotonic()+5
                while pending:
                    retry=[]
                    for path in pending:
                        try:
                            path.rmdir()
                            last_errors.pop(path,None)
                        except FileNotFoundError:
                            last_errors.pop(path,None)
                        except OSError as exc:
                            retry.append(path); last_errors[path]=exc
                    if not retry or time.monotonic()>=deadline:
                        pending=retry; break
                    pending=retry; time.sleep(.1)
            if pending:
                raise RuntimeError('priority recovery cleanup failed: '
                                   +'; '.join(str(last_errors[path]) for path in pending))
            journal.unlink()
