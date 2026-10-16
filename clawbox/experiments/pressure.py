"""Pressure feedback and bounded controls; admission and lifecycle own execution.

All event IDs are local to one run. Completed IDs are tombstones: a delayed
start cannot resurrect an execution. No replay oracle is consumed here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import Event, RLock, Thread
import math
import statistics
import time

from .spec_types import RuntimeControl

MIB = 1024 ** 2


class Phase(StrEnum):
    TOOL_ACTIVE = 'TOOL_ACTIVE'
    LLM_WAITING = 'LLM_WAITING'
    READY = 'READY'
    TRANSITION = 'TRANSITION'
    INACTIVE = 'INACTIVE'


@dataclass
class VMState:
    session: str
    role: str
    group: Path
    node: int | None
    sandbox: str = ''
    generation: int = 0
    phase: Phase = Phase.TRANSITION
    active: set[str] = field(default_factory=set)
    pending: set[str] = field(default_factory=set)
    finished: set[str] = field(default_factory=set)
    waits: set[str] = field(default_factory=set)
    responses: set[str] = field(default_factory=set)
    latest_event: str = 'registered'
    current_bytes: int = 0
    saved: dict[str, str] = field(default_factory=dict)
    previous: dict = field(default_factory=dict)
    action_since: float | None = None
    cooldown_until: float = 0
    modified: set[str] = field(default_factory=set)

    def refresh(self):
        self.phase = (Phase.TOOL_ACTIVE if self.active else Phase.READY if self.pending
                      else Phase.LLM_WAITING if self.waits else Phase.INACTIVE)


def counters(path: Path) -> dict[str, int]:
    return {k: int(v) for k, v in (line.split() for line in path.read_text().splitlines())}


def psi(path: Path) -> int | None:
    if not path.exists():
        return None
    line = next(row for row in path.read_text().splitlines() if row.startswith('some '))
    return int(dict(item.split('=') for item in line.split()[1:])['total'])


class PressureControl:
    def __init__(self, parent, *, mode, options, interval=.5, emit=lambda _: None,
                 memory_protection=False, cpu_release_boost=False):
        self.parent = Path(parent)
        self.mode = RuntimeControl(mode)
        self.memory_protection = memory_protection if self.mode is RuntimeControl.EXPLICIT else self.mode in {RuntimeControl.PHASE_MEMCG, RuntimeControl.ADAPTIVE_JOINT}
        self.cpu_release_boost = cpu_release_boost if self.mode is RuntimeControl.EXPLICIT else self.mode in {RuntimeControl.RELEASE_CPU, RuntimeControl.ADAPTIVE_JOINT}
        self.options = options
        self.interval = interval
        self.emit = emit
        self.lock = RLock()
        self.vms = {}
        self.domains = {}
        self.stop_event = Event()
        self.thread = None
        self.error = None
        self.stats = {'polls': 0, 'poll_seconds': 0., 'cpu_boosts': 0,
                      'memory_protections': 0, 'rollbacks': 0, 'oom_kill_events': 0}
        self.provenance = {'backend': self.mode.value, 'mapping': 'stable_flat_per_vm',
                           'memory_protection_enabled': self.memory_protection,
                           'cpu_release_boost_enabled': self.cpu_release_boost,
                           'memory_high': 'disabled_no_safe_growth_control_evidence',
                           'cpu_release_evidence': 'required',
                           'memory_psi_available': (self.parent/'memory.pressure').exists(),
                           'cpu_psi_available': (self.parent/'cpu.pressure').exists(),
                           **options.model_dump()}
        self.last_pressure = {}
        # Release forecasts must be independently measured and bound to the
        # current execution/generation. Current ClawTune peak demand is NOT one.
        self.release_forecasts = {}
        self.execution_records = {}
        self.release_history = {}
        self.parent_previous = None

    def _psi(self, group, resource):
        path = group / f'{resource}.pressure'
        value = psi(path)
        required = self.memory_protection if resource == 'memory' else self.cpu_release_boost
        if required and value is None:
            raise RuntimeError(
                f'{resource} PSI is required by the enabled runtime control: missing {path}. '
                'Enable PSI on the host (CONFIG_PSI=y; boot with psi=1 if disabled by default), '
                'reboot and verify the cgroup pressure files, or explicitly disable the '
                'corresponding runtime mechanism in the experiment configuration.')
        return value

    def execution_ready(self, session, execution, command_key):
        with self.lock:
            self.execution_records[session, execution] = {'key': command_key}
        self.event(session, 'ready', execution)

    def execution_admitted(self, session, execution, baseline_bytes):
        with self.lock:
            self.execution_records[session, execution]['baseline_bytes'] = baseline_bytes

    def execution_complete(self, session, execution, observation, duration, success):
        with self.lock:
            record = self.execution_records.pop((session, execution), {})
            values = [observation.get('host_cgroup_' + name + '_bytes')
                      for name in ('baseline', 'peak', 'final')]
            if (record.get('key') and success and not observation.get('host_cgroup_error')
                    and all(isinstance(value, int) and value >= 0 for value in values)
                    and math.isfinite(duration) and duration > 0):
                baseline, peak, final = values
                released = max(0, min(peak - baseline, peak - final))
                history = self.release_history.setdefault(record['key'], [])
                history.append((duration, released))
                del history[:-8]
                self.emit({'event': 'pressure_execution_release', 'session_id': session,
                           'execution_id': execution, 'transient_released_bytes': released,
                           'duration_seconds': duration})
        self.event(session, 'complete', execution)

    def register(self, session, role, group, node, coordinator):
        with self.lock:
            vm = VMState(session, role, Path(group), node)
            if role == 'tool':
                self._psi(vm.group, 'memory')
                self._psi(vm.group, 'cpu')
            for name in ('cpu.weight', 'memory.low', 'memory.high'):
                vm.saved[name] = (vm.group / name).read_text().strip()
            if vm.saved != {'cpu.weight': '100', 'memory.low': '0', 'memory.high': 'max'}:
                raise RuntimeError(f'adaptive VM leaf has nondefault controls: {vm.group}')
            self.vms[session, role] = vm
            self.domains[session] = coordinator

    def generation(self, session, role, sandbox):
        with self.lock:
            vm = self.vms[session, role]
            self._restore(vm)
            vm.generation += 1
            vm.sandbox = sandbox
            vm.previous.clear()
            vm.cooldown_until = time.monotonic() + self.options.cooldown_seconds
            vm.refresh()
            vm.latest_event = 'vmm_verified'
            self.emit({'event': 'pressure_vm_generation', 'session_id': session,
                       'role': role, 'generation': vm.generation, 'sandbox_id': sandbox,
                       'cgroup': str(vm.group), 'node': vm.node})

    def transition(self, session, role):
        with self.lock:
            vm = self.vms[session, role]
            self._restore(vm)
            vm.phase = Phase.TRANSITION

    def event(self, session, kind, identifier):
        with self.lock:
            for role in ('runtime', 'tool'):
                vm = self.vms.get((session, role))
                if vm is None:
                    continue
                if kind == 'model_start' and identifier not in vm.responses:
                    vm.waits.add(identifier)
                elif kind == 'model_end':
                    vm.responses.add(identifier)
                    vm.waits.discard(identifier)
                elif role == 'tool':
                    if kind == 'ready' and identifier not in vm.finished:
                        vm.pending.add(identifier)
                    elif kind == 'start' and identifier in vm.pending and identifier not in vm.finished:
                        vm.pending.discard(identifier)
                        vm.active.add(identifier)
                        record = self.execution_records.get((session, identifier), {})
                        history = self.release_history.get(record.get('key'), [])
                        if len(history) >= self.options.release_history_samples:
                            durations = [row[0] for row in history]
                            release = min(row[1] for row in history)
                            if max(durations) <= 2 * min(durations) and release >= self.options.min_release_mib * MIB:
                                now = time.monotonic()
                                self.release_forecasts[session, identifier] = {
                                    'generation': vm.generation,
                                    'expires_at': now + max(durations) * self.options.release_forecast_ttl_factor,
                                    'expected_complete_at': now + statistics.median(durations),
                                    'releasable_bytes': release,
                                    'baseline_bytes': record.get('baseline_bytes'),
                                    'release_overhead_seconds': 0.,
                                    'source': 'same_template_command_measured_transient_release'}
                    elif kind == 'complete':
                        vm.finished.add(identifier)
                        vm.active.discard(identifier)
                        vm.pending.discard(identifier)
                        self.release_forecasts.pop((session, identifier), None)
                vm.latest_event = f'{kind}:{identifier}'
                vm.refresh()
                # End-of-tool/model conditions must remove biases immediately.
                if kind in ('complete', 'model_end'):
                    self._restore(vm)

    def unregister(self, session):
        with self.lock:
            for role in ('runtime', 'tool'):
                vm = self.vms.pop((session, role), None)
                if vm:
                    self._restore(vm)
            self.domains.pop(session, None)

    def _restore(self, vm):
        if vm.action_since is not None:
            vm.cooldown_until = max(vm.cooldown_until, time.monotonic() + self.options.cooldown_seconds)
        for name in tuple(vm.modified):
            value = vm.saved[name]
            path = vm.group / name
            if path.exists():
                path.write_text(value)
                self.stats['rollbacks'] += 1
            vm.modified.discard(name)
        vm.action_since = None

    def _sample(self, vm, now):
        stat = counters(vm.group / 'memory.stat')
        current = int((vm.group / 'memory.current').read_text())
        events = counters(vm.group / 'memory.events.local')
        sample = {'at': now, 'memory_psi': self._psi(vm.group, 'memory'),
                  'cpu_psi': self._psi(vm.group, 'cpu'),
                  'cpu': counters(vm.group / 'cpu.stat').get('usage_usec', 0),
                  'scan': stat.get('pgscan', 0), 'oom': events.get('oom_kill', 0)}
        previous = vm.previous
        vm.previous = sample
        vm.current_bytes = current
        dt = now - previous.get('at', now)
        def stalled(key):
            return (dt > 0 and sample[key] is not None and previous.get(key) is not None
                    and (sample[key] - previous[key]) / (dt * 1e6) >= self.options.psi_ratio)
        self.stats['oom_kill_events'] += max(0, sample['oom'] - previous.get('oom', 0))
        return {'reclaim': stalled('memory_psi') and sample['scan'] > previous.get('scan', sample['scan']),
                'cpu_contended': stalled('cpu_psi') and sample['cpu'] > previous.get('cpu', sample['cpu']),
                'working_set': min(current, stat.get('anon', 0) + stat.get('active_file', 0)),
                'current_bytes': current, 'events': events, 'stat': stat,
                'memory_psi_total': sample['memory_psi'], 'cpu_psi_total': sample['cpu_psi']}

    def poll(self):
        began = time.monotonic()
        parent_sample = (began, self._psi(self.parent, 'memory'),
                         counters(self.parent / 'memory.stat').get('pgscan', 0))
        previous = self.parent_previous
        self.parent_previous = parent_sample
        reclaim_contended = bool(previous and parent_sample[1] is not None and previous[1] is not None
            and began > previous[0] and parent_sample[2] > previous[2]
            and (parent_sample[1] - previous[1]) / ((began - previous[0]) * 1e6) >= self.options.psi_ratio)
        # Never hold our lock while acquiring the admission lock.
        with self.lock:
            domains = dict(self.domains)
        feedback = {coordinator: coordinator.resource_feedback()
                    for coordinator in set(domains.values())}
        with self.lock:
            protection_left = int(self.options.protection_budget_mib * MIB)
            boosts_left = self.options.max_boosted_vms
            def score(vm):
                forecasts = [self.release_forecasts.get((vm.session, eid), {}) for eid in vm.active]
                # A late execution is not evidence of imminent completion. Bound
                # its score by one action window instead of the TTL deadline.
                return max((f.get('releasable_bytes', 0) /
                            max(self.options.action_window_seconds,
                                f['expected_complete_at'] - began + f.get('release_overhead_seconds', 0))
                            for f in forecasts if f.get('expires_at', 0) > began
                            and f.get('generation') == vm.generation), default=0)
            for vm in sorted(self.vms.values(), key=score, reverse=True):
                domain = domains.get(vm.session)
                signal = feedback.get(domain, {})
                blocked = signal.get('memory_blocked', False)
                previous_pressure = self.last_pressure.get(domain, 0)
                if blocked:
                    self.last_pressure[domain] = began
                contended = blocked or reclaim_contended or began - previous_pressure < self.options.exit_hysteresis_seconds
                if vm.role != 'tool' or vm.phase is not Phase.TOOL_ACTIVE:
                    self._restore(vm)
                    continue
                if not contended:
                    self._restore(vm)
                    vm.previous.clear()
                    continue
                # Active leaves only: no per-request scans or reads of idle VMs.
                sample = self._sample(vm, began)
                if (not contended or began < vm.cooldown_until
                        or (vm.action_since is not None
                            and began - vm.action_since >= self.options.action_window_seconds)):
                    if vm.action_since is not None:
                        vm.cooldown_until = began + self.options.cooldown_seconds
                    self._restore(vm)
                    continue
                low = 0
                if self.memory_protection and sample['reclaim']:
                    low = min(sample['working_set'], protection_left)
                    protection_left -= low
                weight = 100
                if (blocked and boosts_left and sample['cpu_contended']
                        and vm.node is not None
                        and (vm.group / 'cpuset.mems.effective').read_text().strip() == str(vm.node)
                        and self.cpu_release_boost):
                    forecasts = [self.release_forecasts.get((vm.session, eid), {}) for eid in vm.active]
                    valid = [f for f in forecasts if f.get('generation') == vm.generation
                             and f.get('expires_at', 0) > began
                             and f.get('releasable_bytes', 0) >= self.options.min_release_mib * MIB
                             and isinstance(f.get('baseline_bytes'), int)
                             and sample['current_bytes'] - f['baseline_bytes'] >= self.options.min_release_mib * MIB
                             and f.get('release_overhead_seconds', -1) >= 0]
                    if valid:
                        weight = self.options.boost_cpu_weight
                        boosts_left -= 1
                for name, value, metric in (('memory.low', str(low), 'memory_protections'),
                                            ('cpu.weight', str(weight), 'cpu_boosts')):
                    path = vm.group / name
                    if path.read_text().strip() != value:
                        vm.modified.add(name)
                        path.write_text(value)
                        self.stats[metric] += int(value not in ('0', '100'))
                        self.emit({'event': 'pressure_control_action', 'session_id': vm.session,
                                   'generation': vm.generation, 'control': name, 'value': value,
                                   'phase': vm.phase.value, 'feedback': signal, 'sample': sample})
                if low or weight != 100:
                    vm.action_since = vm.action_since or began
                else:
                    if vm.action_since is not None:
                        vm.cooldown_until = began + self.options.cooldown_seconds
                    vm.action_since = None
            self.stats['polls'] += 1
            self.stats['poll_seconds'] += time.monotonic() - began

    def check(self):
        if self.error:
            raise RuntimeError('pressure controller failed; controls restored') from self.error
        if self.stop_event.is_set():
            raise RuntimeError('pressure controller stopped')

    def check_prerequisites(self):
        self._psi(self.parent, 'memory')
        self._psi(self.parent, 'cpu')

    def start(self):
        self.check_prerequisites()
        self.emit({'event': 'runtime_memory_started', **self.provenance})
        if not self.mode.adaptive:
            return
        def run():
            try:
                while not self.stop_event.wait(self.interval):
                    self.poll()
            except Exception as exc:
                self.error = exc
                with self.lock:
                    for vm in self.vms.values():
                        try:
                            self._restore(vm)
                        except Exception as restore_error:
                            exc.add_note(str(restore_error))
        self.thread = Thread(target=run, name='pressure-progress', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError('pressure controller did not stop')
        with self.lock:
            for vm in self.vms.values():
                self._restore(vm)
        self.emit({'event': 'runtime_memory_stopped', **self.provenance, **self.stats})
        if self.error:
            raise RuntimeError('pressure controller failed') from self.error
