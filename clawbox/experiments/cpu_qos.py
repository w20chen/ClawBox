"""Host vCPU scheduling for explicitly labelled experiment sessions."""
from __future__ import annotations

import ctypes
import os
import time
from concurrent.futures import Future
from functools import lru_cache
from queue import Queue
from pathlib import Path
from threading import Lock, Thread


class CpuQosUsageSampler:
    """Observed CPU ticks of verified vCPUs; departed-thread tails stay unknown."""

    def __init__(self, capacity_cpus: str, *, proc_root: Path = Path('/proc'),
                 clock=time.monotonic, clock_ticks_per_second: int | None = None):
        from .topology import parse_cpu_list
        self.capacity_cpus = capacity_cpus
        self.logical_cpus = len(parse_cpu_list(capacity_cpus))
        if not self.logical_cpus:
            raise ValueError('CPU QoS usage requires an explicit nonempty CPU capacity mask')
        self.proc_root, self.clock = proc_root, clock
        self.hz = clock_ticks_per_second or os.sysconf('SC_CLK_TCK')
        if self.hz <= 0:
            raise ValueError('CPU accounting clock ticks must be positive')
        self._lock = Lock()
        self._threads, self._vm_keys, self._tid_keys = {}, {}, {}
        self._pending = {q: dict(new_threads=0, departed_threads=0,
                                lost_exit_tail_threads=0, registration_failures=0)
                         for q in ('ls', 'be')}
        self._total_ticks = dict(ls=0, be=0)
        self._total_samples = dict(ls=0, be=0)
        self._total_lost = dict(ls=0, be=0)
        self._last_sample = clock()
        self._sample_epoch = 0

    def _read(self, tid):
        try:
            # comm may contain spaces or ')'; fields after its final ')' start
            # at field 3, making utime/stime/starttime indexes 11/12/19.
            # The task directory explicitly selects per-thread accounting.
            # On the experiment host /proc/TID/stat exposes thread-group totals.
            fields = (self.proc_root / str(tid) / 'task' / str(tid) / 'stat').read_text().rsplit(')', 1)[1].split()
            return (tid, int(fields[19])), int(fields[11]) + int(fields[12])
        except (OSError, ValueError, IndexError):
            return None

    def _drop(self, key):
        vm_key, qos_class, *_ = self._threads.pop(key)
        self._vm_keys[vm_key].discard(key)
        self._tid_keys.pop(key[0], None)
        self._pending[qos_class]['departed_threads'] += 1
        self._pending[qos_class]['lost_exit_tail_threads'] += 1

    def register(self, vm_key, qos_class: str, tids) -> None:
        if qos_class not in self._pending:
            return
        with self._lock:
            read_epoch = self._sample_epoch
        readings = [self._read(int(tid)) for tid in set(tids)]
        present = {item[0] for item in readings if item is not None}
        with self._lock:
            self._pending[qos_class]['registration_failures'] += sum(item is None for item in readings)
            for key in self._vm_keys.get(vm_key, set()) - present:
                self._drop(key)
            self._vm_keys.setdefault(vm_key, set())
            for reading in readings:
                if reading is None:
                    continue
                key, ticks = reading
                previous = self._tid_keys.get(key[0])
                if previous is not None and (previous != key or
                        self._threads[previous][:2] != (vm_key, qos_class)):
                    self._drop(previous)
                if key not in self._threads:
                    self._threads[key] = (vm_key, qos_class, ticks, read_epoch)
                    self._tid_keys[key[0]] = key
                    self._vm_keys[vm_key].add(key)
                    self._pending[qos_class]['new_threads'] += 1

    def sample(self) -> dict:
        read_started = self.clock()
        with self._lock:
            snapshot = dict(self._threads)
        readings = {key: self._read(key[0]) for key in snapshot}
        sampled_at = self.clock()
        ticks, observed = dict(ls=0, be=0), dict(ls=0, be=0)
        crossed_boundary = dict(ls=0, be=0)
        with self._lock:
            elapsed = sampled_at - self._last_sample
            self._last_sample = sampled_at
            epoch = self._sample_epoch
            self._sample_epoch += 1
            for key, state in snapshot.items():
                if self._threads.get(key) != state:
                    continue  # registration changed ownership during proc I/O
                vm_key, qos_class, previous_ticks, read_epoch = state
                reading = readings[key]
                if reading is None or reading[0] != key or reading[1] < previous_ticks:
                    self._drop(key)
                    continue
                ticks[qos_class] += reading[1] - previous_ticks
                observed[qos_class] += 1
                crossed_boundary[qos_class] += read_epoch != epoch
                self._threads[key] = (vm_key, qos_class, reading[1], epoch + 1)
            classes = {}
            for qos_class in ('ls', 'be'):
                changes = self._pending[qos_class]
                missing = changes['lost_exit_tail_threads'] + changes['registration_failures']
                unaligned = crossed_boundary[qos_class] + sum(
                    state[1] == qos_class and state[3] != epoch + 1
                    for state in self._threads.values())
                self._total_ticks[qos_class] += ticks[qos_class]
                self._total_samples[qos_class] += observed[qos_class]
                self._total_lost[qos_class] += missing
                seconds = ticks[qos_class] / self.hz if observed[qos_class] or not (missing or unaligned) else None
                load = seconds / elapsed / self.logical_cpus if seconds is not None and elapsed > 0 else None
                classes[qos_class] = {
                    **changes, 'sampled_threads': observed[qos_class],
                    'unaligned_interval_threads': unaligned,
                    'registered_threads': sum(state[1] == qos_class for state in self._threads.values()),
                    'observed_cpu_seconds': seconds,
                    'observed_load_fraction': load if not (missing or unaligned) else None,
                    'observed_load_fraction_lower_bound': load if missing and not unaligned else None,
                    'coverage': 'partial' if missing or unaligned else 'complete',
                    'lost_exit_tail': bool(changes['lost_exit_tail_threads']),
                    'cumulative_observed_cpu_seconds': (
                        self._total_ticks[qos_class] / self.hz
                        if self._total_samples[qos_class] or not self._total_lost[qos_class] else None),
                    'cumulative_coverage': 'partial' if self._total_lost[qos_class] else 'complete',
                }
                self._pending[qos_class] = dict.fromkeys(changes, 0)
        return {'measurement': 'host_vcpu_proc_stat', 'scope': 'verified_runtime_and_tool_vcpu_threads',
                'capacity_cpus': self.capacity_cpus, 'allowed_logical_cpus': self.logical_cpus,
                'elapsed_seconds': elapsed, 'sample_read_seconds': sampled_at - read_started,
                'classes': classes}


# Linux UAPI include/uapi/linux/prctl.h; every operation here uses THREAD scope.
PR_SCHED_CORE = 62
PR_SCHED_CORE_GET = 0
PR_SCHED_CORE_CREATE = 1
PR_SCHED_CORE_SHARE_TO = 2
PR_SCHED_CORE_SCOPE_THREAD = 0


@lru_cache(maxsize=1)
def _prctl():
    try:
        function = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError) as exc:
        raise RuntimeError('native core scheduling requires Linux prctl') from exc
    function.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                         ctypes.c_ulong, ctypes.c_void_p]
    function.restype = ctypes.c_int
    return function


def _core_command(command: int, tid: int = 0, result=None) -> None:
    if _prctl()(PR_SCHED_CORE, command, tid, PR_SCHED_CORE_SCOPE_THREAD, result) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f'PR_SCHED_CORE command {command}, tid {tid}: {os.strerror(error)}')


def get_core_cookie(tid: int = 0) -> int:
    cookie = ctypes.c_ulonglong()
    _core_command(PR_SCHED_CORE_GET, tid, ctypes.byref(cookie))
    return cookie.value


class _CoreCookieAnchor:
    """Only this dedicated thread ever adopts its class cookie."""

    def __init__(self, qos_class: str):
        self.qos_class = qos_class
        self.cookie = None
        self.requests = Queue()
        self.ready = Future()
        self.thread = Thread(target=self._run, name=f'cpu-qos-{qos_class}', daemon=True)

    def start(self) -> None:
        self.thread.start()
        self.cookie = self.ready.result(timeout=10)

    def _run(self) -> None:
        try:
            _core_command(PR_SCHED_CORE_CREATE)
            cookie = get_core_cookie()
            if not cookie:
                raise RuntimeError(f'core scheduling created a zero {self.qos_class} cookie')
            self.ready.set_result(cookie)
        except BaseException as exc:
            self.ready.set_exception(exc)
            return
        while True:
            request = self.requests.get()
            if request is None:
                return
            tids, result = request
            try:
                observed = {}
                for tid in tids:
                    _core_command(PR_SCHED_CORE_SHARE_TO, tid)
                    observed[tid] = get_core_cookie(tid)
                    if observed[tid] != cookie:
                        raise RuntimeError(
                            f'core cookie mismatch for host vCPU {tid}: '
                            f'expected {cookie}, observed {observed[tid]}'
                        )
                result.set_result(observed)
            except BaseException as exc:
                result.set_exception(exc)

    def assign(self, tids: tuple[int, ...]) -> dict[int, int]:
        if not self.thread.is_alive() or self.cookie is None:
            raise RuntimeError(f'{self.qos_class} core scheduling anchor is not running')
        result = Future()
        self.requests.put((tids, result))
        return result.result(timeout=10)

    def close(self) -> None:
        if self.thread.ident is None:
            return
        self.requests.put(None)
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError(f'{self.qos_class} core scheduling anchor did not stop')


class CoreSchedulingGroups:
    """Two native class cookies, shared without changing worker-thread cookies.

    Anchors remain alive across VM eviction and restore. They spawn no children;
    callers never pull a cookie or change their own thread/group cookie.
    """

    def __init__(self):
        self.anchors: dict[str, _CoreCookieAnchor] = {}

    def start(self) -> None:
        if self.anchors:
            raise RuntimeError('core scheduling groups are already started')
        check_cpu_qos_support('idle_core')
        caller_cookie = get_core_cookie()
        try:
            for qos_class in ('ls', 'be'):
                anchor = _CoreCookieAnchor(qos_class)
                self.anchors[qos_class] = anchor
                anchor.start()
            if self.anchors['ls'].cookie == self.anchors['be'].cookie:
                raise RuntimeError('LS and BE core scheduling cookies must differ')
            if get_core_cookie() != caller_cookie:
                raise RuntimeError('core scheduling unexpectedly changed the worker cookie')
        except BaseException:
            self.close()
            raise

    def assign(self, qos_class: str, tids: tuple[int, ...]) -> dict[int, int]:
        if qos_class not in self.anchors:
            raise RuntimeError(f'no active native core scheduling group for {qos_class}')
        try:
            return self.anchors[qos_class].assign(tids)
        except OSError as exc:
            raise RuntimeError(
                f'native core scheduling failed for {qos_class} vCPUs: {exc}; '
                'verify CONFIG_SCHED_CORE and host ptrace/scheduler privileges'
            ) from exc

    def close(self) -> None:
        errors = []
        for anchor in self.anchors.values():
            try:
                anchor.close()
            except RuntimeError as exc:
                errors.append(str(exc))
        self.anchors.clear()
        if errors:
            raise RuntimeError('; '.join(errors))


def check_cpu_qos_support(policy: str) -> dict:
    """Check Linux scheduling interfaces without modifying the calling thread.

    This is an interface check, not proof of permission to modify a VM thread;
    applying and reading back each real vCPU policy provides that evidence.
    """
    if policy not in {'off', 'idle', 'idle_core'}:
        raise ValueError(f'unsupported CPU QoS policy: {policy}')
    if policy == 'off':
        return {'policy': policy, 'probe': 'not_required'}
    required = ('SCHED_IDLE', 'SCHED_OTHER', 'sched_setscheduler',
                'sched_getscheduler', 'sched_get_priority_min', 'sched_param')
    if any(not hasattr(os, name) for name in required):
        raise RuntimeError('CPU QoS idle requires Linux SCHED_IDLE scheduling interfaces')
    try:
        os.sched_get_priority_min(os.SCHED_IDLE)
        os.sched_getscheduler(0)
    except OSError as exc:
        raise RuntimeError(f'Linux SCHED_IDLE capability check failed: {exc}') from exc
    evidence = {'policy': policy, 'probe': 'linux_scheduler_interfaces',
                'vm_thread_permission_verified': False}
    if policy == 'idle_core':
        try:
            evidence['caller_core_cookie'] = get_core_cookie()
        except OSError as exc:
            raise RuntimeError(
                f'native core scheduling is unavailable: {exc}; '
                'boot a kernel with CONFIG_SCHED_CORE=y before running idle_core; '
                'no fallback is permitted'
            ) from exc
    return evidence


def restore_group_idle(parent: Path, originals: dict) -> None:
    """Restore only the native VM groups recorded by this experiment."""
    for rendered, values in originals.items():
        group = Path(rendered).resolve()
        if group.parent != parent.resolve() or not group.name.isdecimal():
            raise ValueError(f'Invalid owned VM CPU cgroup: {group}')
        if not group.exists():
            continue
        (group/'cpu.idle').write_text(str(values['idle']))
        if not values['idle']:
            (group/'cpu.weight').write_text(str(values['weight']))
        if int((group/'cpu.idle').read_text()) != values['idle']:
            raise RuntimeError(f'Failed to restore CPU cgroup policy at {group}')


def apply_group_idle(cgroup: Path, *, qos_class: str) -> dict:
    """Make BE VM groups idle relative to sibling LS groups, not just their threads."""
    if qos_class not in {'ls', 'be'}:
        raise ValueError('Group CPU QoS requires an explicit ls or be class')
    path = cgroup/'cpu.idle'
    desired = int(qos_class == 'be')
    try:
        before = int(path.read_text().strip())
        if before != desired:
            path.write_text(str(desired))
        observed = int(path.read_text().strip())
        weight = int((cgroup/'cpu.weight').read_text().strip())
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f'Cannot apply VM group CPU QoS at {cgroup}; enable the cgroup v2 '
            'CPU controller with cpu.idle support and run with host privileges'
        ) from exc
    # cpu.idle is the policy readback. The displayed weight is kernel-dependent
    # (official openEuler 145 reports 1 for an idle group); retain it as evidence.
    if observed != desired:
        raise RuntimeError(f'VM group CPU QoS verification failed: cpu.idle={observed}, cpu.weight={weight}')
    return {'path': str(cgroup), 'requested_idle': desired, 'effective_idle': observed,
            'effective_weight': weight, 'scope': 'vm_cgroup_including_helpers'}


def apply_vcpu_qos(threads: list[dict], *, policy: str, qos_class: str,
                   core_groups: CoreSchedulingGroups | None = None) -> dict:
    """Apply BE SCHED_IDLE and verify every observed vCPU after create/restore."""
    if policy not in {'off', 'idle', 'idle_core'} or qos_class not in {'neutral', 'ls', 'be'}:
        raise ValueError(f'invalid CPU QoS selection: policy={policy}, class={qos_class}')
    if policy == 'idle_core' and (qos_class == 'neutral' or core_groups is None):
        raise RuntimeError('idle_core requires an explicit ls/be class and active native core groups')
    evidence = {'policy': policy, 'qos_class': qos_class,
                'scope': 'host_vcpu_threads', 'threads': []}
    # Keep unlabelled existing experiments unchanged, including their probes.
    if policy == 'off' and qos_class == 'neutral':
        return evidence
    if not threads:
        raise RuntimeError('CPU QoS cannot be verified without host vCPU threads')
    if not all(hasattr(os, name) for name in ('SCHED_OTHER', 'sched_getscheduler')):
        raise RuntimeError('CPU QoS verification requires Linux scheduling interfaces')
    idle = policy in {'idle', 'idle_core'} and qos_class == 'be'
    if idle:
        check_cpu_qos_support(policy)
    target = os.SCHED_IDLE if idle else os.SCHED_OTHER
    expected_name = 'SCHED_IDLE' if idle else 'SCHED_OTHER'
    for thread in threads:
        tid = int(thread['tid'])
        try:
            if idle:
                os.sched_setscheduler(tid, target, os.sched_param(0))
            observed = os.sched_getscheduler(tid)
        except OSError as exc:
            raise RuntimeError(
                f'CPU QoS {policy}/{qos_class} failed for host vCPU {tid}: {exc}; '
                'run the experiment with host scheduler privileges and verify the VM is alive'
            ) from exc
        if observed != target:
            raise RuntimeError(
                f'CPU QoS mismatch for host vCPU {tid}: expected {expected_name} '
                f'({target}), observed scheduler {observed}; '
                'check inherited VM scheduling policy and host scheduler permissions'
            )
        evidence['threads'].append({
            'tid': tid, 'name': thread['name'],
            'requested_scheduler': expected_name, 'effective_scheduler': expected_name,
            'scheduler': observed,
        })
    if policy == 'idle_core':
        cookies = core_groups.assign(qos_class, tuple(int(thread['tid']) for thread in threads))
        for thread in evidence['threads']:
            thread['effective_core_cookie'] = cookies[thread['tid']]
        evidence['core_cookie'] = cookies[int(threads[0]['tid'])]
    return evidence
