"""Persistent official AgentCgroup runtime control, independent of admission.

The pinned AgentCgroup source is loaded unchanged. Integration adaptations and
source provenance are recorded with each arm.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
from pathlib import Path
import sys
from threading import Event, Thread
import time
from typing import Callable

UPSTREAM_SHA256 = '5fcb60bea882aa1bd19f8c646296630ca5425cd07d9bed1eb7b684231aeee62c'
UPSTREAM_COMMIT = '551ca1689b7d2d30db6b8a8613414750103cc68b'
UPSTREAM_SOURCE = Path(__file__).parent/'vendor'/'agentcgroup'/'memcg_controller.py'
MIB = 1024**2


def locate_cgroup(sandbox_id: str, parent: Path, proc: Path = Path('/proc')) -> Path:
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            argv = (p/'cmdline').read_bytes().split(b'\0')
            if b'-id' not in argv or argv[argv.index(b'-id')+1] != sandbox_id.encode():
                continue
            if not any(x.endswith(b'containerd-shim-cube-rs') for x in argv):
                continue
            relative = next(line[3:] for line in (p/'cgroup').read_text().splitlines() if line.startswith('0::'))
            group = Path('/sys/fs/cgroup')/relative.lstrip('/')
            group = group.resolve()
            if group == parent.resolve() or not group.is_relative_to(parent.resolve()):
                raise RuntimeError('Cube VM is outside the configured memory domain')
            return group
        except (FileNotFoundError, ProcessLookupError):
            continue
    raise RuntimeError(f'no owned Cube VMM cgroup found for {sandbox_id}')


def _stats(group: Path) -> dict[str, int]:
    return {k: int(v) for k, v in (line.split() for line in (group/'memory.stat').read_text().splitlines())}


class RuntimeMemoryControl:
    """One independent control clock for the shared physical VM memory domain.

    No prediction, execution ticket, or admission queue is owned here.
    NUMA placement/borrowing remains owned by the existing watermark controller.
    """

    def __init__(self, parent: Path, source: Path | None = None, *, high: Path, low: Path, enabled: bool = True,
                 interval: float = .1, emit: Callable[[dict], None] = lambda _: None):
        self.parent = parent.resolve()
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError('runtime control interval must be positive and finite')
        limit = (parent/'memory.max').read_text().strip()
        if limit == 'max' or int(limit) <= 0:
            raise ValueError('AgentCgroup requires a finite positive physical memory.max; apply the host budget first')
        self.capacity = int(limit)
        source = source or UPSTREAM_SOURCE
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != UPSTREAM_SHA256:
            raise ValueError('AgentCgroup source hash differs from the pinned audited backend')
        name = '_clawbox_agentcgroup_' + str(id(self))
        spec = importlib.util.spec_from_file_location(name, source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        def checked_write(path, filename, value):
            (Path(path)/filename).write_text(value)
            return True
        module._cgroup_write = checked_write
        # Upstream treats read failures as absence of pressure. Experiments must
        # instead fail, or an unsupported/removed cgroup could look successful.
        def checked_read(path, filename):
            target = Path(path)/filename
            # PSI is an optional kernel feature in the upstream fallback.
            # Preserve its two other signals on kernels built without PSI;
            # disappearance of the cgroup itself is still a fatal error.
            if filename == 'memory.pressure' and not target.exists() and (Path(path)/'memory.events').exists():
                return None
            return target.read_text().strip()
        module._cgroup_read = checked_read
        self.oom_before = module._read_memory_events(str(self.parent)).get('oom_kill', 0)
        self.module = module
        self.module_name = name
        self.high, self.low = high.resolve(), low.resolve()
        if self.high.parent != self.parent or self.low.parent != self.parent or self.high == self.low:
            raise ValueError('HIGH and LOW must be distinct children of the physical domain')
        self.backend = module.CgroupMemcgController() if enabled else None
        self.attached = False
        self.interval = interval
        self.emit = emit
        self.saved = {p: ((p/'memory.low').read_text().strip(), (p/'memory.high').read_text().strip())
                      for p in (self.high, self.low)}
        self.stop_event = Event()
        self.thread = None
        self.error: Exception | None = None
        self.stats = {'polls': 0, 'poll_seconds': 0., 'max_poll_gap_seconds': 0.}
        self.last_poll = None
        self.provenance = {'backend': 'agentcgroup_cgroup' if enabled else 'off', 'commit': UPSTREAM_COMMIT,
                           'source_sha256': digest,
                           'capacity_bytes': self.capacity, 'mapping': 'fixed_session_high_low_aggregate',
                           'high_cgroup': str(self.high), 'low_cgroup': str(self.low),
                           'interval_seconds': interval, 'threshold': 1, 'protection_window_seconds': 1.0,
                           'psi_available': (parent/'memory.pressure').exists(),
                           'adaptations': ['strict_cgroup_io', 'independent_poll_clock', 'vm_session_descendants']}

    def check(self):
        if self.error:
            raise RuntimeError('runtime memory controller failed') from self.error
        if self.stop_event.is_set():
            raise RuntimeError('runtime memory controller stopped')

    def _poll_backend(self):
        if (self.parent/'memory.max').read_text().strip() != str(self.capacity):
            raise RuntimeError('physical memory.max changed during the arm')
        if self.backend:
            began = time.monotonic()
            if self.last_poll is not None:
                self.stats['max_poll_gap_seconds'] = max(self.stats['max_poll_gap_seconds'], began-self.last_poll)
            self.last_poll = began
            self.backend.poll()
            self.stats['polls'] += 1
            self.stats['poll_seconds'] += time.monotonic()-began
            self.stats['upstream'] = self.backend.get_stats()
            self.emit({'event': 'runtime_memory_poll', 'monotonic_s': began,
                       **self.stats['upstream'],
                       'high_memory_low': (self.high/'memory.low').read_text().strip(),
                       'low_memory_high': (self.low/'memory.high').read_text().strip(),
                       'high_events': self.module._read_memory_events(str(self.high)),
                       'low_events': self.module._read_memory_events(str(self.low)),
                       'parent_current_bytes': int((self.parent/'memory.current').read_text())})

    def start(self):
        if self.thread is not None:
            raise RuntimeError('runtime memory controller already started')
        if self.backend:
            # Attach exactly once, before any VM starts. Tool/session events
            # never reset the pressure baselines or active protection window.
            self.attached = True
            if not self.backend.attach(self.module.MemcgConfig(
                    high_cgroup=str(self.high), low_cgroups=[str(self.low)])):
                raise RuntimeError('AgentCgroup attach failed')
        self.emit({'event': 'runtime_memory_started', **self.provenance})
        def run():
            try:
                while not self.stop_event.wait(self.interval):
                    # memory.high writes may synchronously reclaim; do not hold
                    # the admission/completion ledger lock across those writes.
                    self._poll_backend()
            except Exception as exc:
                self.error = exc
        self.thread = Thread(target=run, name='runtime-memory', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError('runtime memory controller did not stop')
        errors = []
        try:
            self.stats['oom_kill_events'] = max(0,
                self.module._read_memory_events(str(self.parent)).get('oom_kill', 0) - self.oom_before)
        except Exception as exc:
            errors.append(str(exc))
        if self.backend and self.attached:
            try:
                self.stats['upstream'] = self.backend.get_stats()
                self.backend.detach()
                self.attached = False
            except Exception as exc:
                errors.append(str(exc))
        for group, (low, high) in self.saved.items():
            if not group.exists():
                continue
            try:
                (group/'memory.low').write_text(low)
                (group/'memory.high').write_text(high)
            except OSError as exc:
                errors.append(str(exc))
        self.emit({'event': 'runtime_memory_stopped', **self.provenance, **self.stats})
        sys.modules.pop(self.module_name, None)
        if self.error or errors:
            raise RuntimeError('runtime memory control/restore failed: '+str(errors)) from self.error
