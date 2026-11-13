"""Original C2 placement baseline, independent of B's prediction policy.

Decision logic retained from 957f4356fd6b620d1d37054244415da6815b0dc1:
elapsed trigger, historical CPU accounting, sample-count PSI expansion, and
slots held until the last active call. Common VM/cgroup enforcement stays in
PriorityDomain. Do not apply B optimizations here: they change the control arm.
"""
from __future__ import annotations

import re
import time
from .control_timing import timed_lock


def cpu_times(path):
    """Retain C2's original accounting, including guest double counting."""
    result = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if not fields or not re.fullmatch(r'cpu\d+', fields[0]):
            continue
        values = [int(value) for value in fields[1:]]
        total = sum(values)
        result[int(fields[0][3:])] = total - sum(values[3:5]), total
    return result


class C2Placement:
    """Policy methods called with the shared PriorityDomain state."""
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

    def _assign_tool_call(self, index: int, execution_id: str, *, vcpus: int,
                          cpu_observation=None) -> dict:
        """Narrow one new call after any minimum-duration delay has elapsed."""
        from .priority import physical_cluster_groups, _pressure_total
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
        measured_load = (self._sample_group_utilization(groups, observation=cpu_observation)
                         if cpu_observation is not None else self._sample_group_utilization(groups))
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

    def sample_topology_placement(self) -> list[dict]:
        from .priority import _pressure_total
        if self.topology_mode == 'c0':
            return []
        now = time.monotonic()
        changes = []
        with self._topology_lock:
            if self._topology_stopping:
                return []
            pending = tuple(self._pending_started.items())
            cpu_observation = self._cpu_observation()
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
                event = self._assign_tool_call(key[0], primary, vcpus=vcpus,
                                              cpu_observation=cpu_observation)
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
            measured_load = self._sample_group_utilization(
                sorted(self._candidate_groups), observation=cpu_observation,
            )
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
            'trigger': 'elapsed_time',
            'slot_accounting': 'until_last_active_call',
            'cpu_accounting': 'historical_including_guest',
            'poll_interval_seconds': self.topology_poll_interval_seconds,
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
