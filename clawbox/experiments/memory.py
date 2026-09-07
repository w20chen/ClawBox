from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from collections.abc import Callable


def read_meminfo(path: Path) -> tuple[int, int]:
    if not path.exists():
        import psutil
        memory = psutil.virtual_memory()
        return int(memory.total), int(memory.available)
    values: dict[str, int] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        key, _, rest = line.partition(":")
        if key in {"MemTotal", "MemAvailable"}:
            values[key] = int(rest.split()[0]) * 1024
    return values["MemTotal"], values["MemAvailable"]


def read_vmstat_counter(path: Path, key: str) -> int:
    if not path.exists():
        return 0
    for line in path.read_text(encoding="ascii").splitlines():
        name, _, raw = line.partition(" ")
        if name == key:
            return int(raw.strip())
    return 0


def read_numa_meminfo(path: Path) -> tuple[int, int]:
    values: dict[str, int] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[2] in {"MemTotal:", "MemFree:"}:
            values[fields[2].removesuffix(":")] = int(fields[3]) * 1024
    if set(values) != {"MemTotal", "MemFree"}:
        raise ValueError(f"incomplete NUMA meminfo: {path}")
    return values["MemTotal"], values["MemFree"]


@dataclass(frozen=True, slots=True)
class MemorySummary:
    mem_total_bytes: int
    baseline_used_bytes: int
    mean_used_delta_bytes: float
    peak_used_delta_bytes: int
    memory_time_integral_byte_seconds: float
    minimum_available_bytes: int
    storage_used_delta_bytes: int
    host_oom_kill_events: int


class NodeMemorySampler:
    sample_hook: Callable[[int, int], None] | None = None

    def __init__(self, *, meminfo: Path = Path("/host/proc/meminfo"),
                 vmstat: Path = Path("/host/proc/vmstat"),
                 storage: Path = Path("/data/cubelet"), interval_s: float = 0.2) -> None:
        self.meminfo = meminfo if meminfo.exists() else Path("/proc/meminfo")
        self.vmstat = vmstat if vmstat.exists() else Path("/proc/vmstat")
        self.storage = storage
        self.interval_s = interval_s
        self._stop = Event()
        self._lock = Lock()
        self._samples: list[tuple[float, int, int]] = []
        self._thread: Thread | None = None
        total, available = read_meminfo(self.meminfo)
        self.total = total
        self.baseline_used = total - available
        self.storage_used_before = self._storage_used()
        self.oom_kill_before = read_vmstat_counter(self.vmstat, "oom_kill")

    def current(self) -> tuple[int, int]:
        total, available = read_meminfo(self.meminfo)
        return max(0, total - available - self.baseline_used), available

    def observe(self) -> dict[str, int | str]:
        """Return an explicit host-memory observation for lifecycle evidence.

        ``host_used_bytes`` is whole-host physical memory in use, whereas
        ``experiment_used_delta_bytes`` is relative to the arm baseline used
        by admission control.  Neither value is guest Tool cgroup memory.
        """
        total, available = read_meminfo(self.meminfo)
        used = max(0, total - available)
        return {
            "metric": "host_meminfo_memavailable",
            "host_mem_total_bytes": total,
            "host_used_bytes": used,
            "host_available_bytes": available,
            "experiment_used_delta_bytes": max(0, used - self.baseline_used),
        }

    def start(self) -> None:
        delta, available = self.current()
        with self._lock:
            self._samples.append((time.monotonic(), delta, available))
        self._thread = Thread(target=self._run, name="node-memory-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> MemorySummary:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        delta, available = self.current()
        with self._lock:
            self._samples.append((time.monotonic(), delta, available))
            samples = list(self._samples)
        if not samples:
            delta, available = self.current()
            samples = [(time.monotonic(), delta, available)]
        integral = sum((samples[index][0] - samples[index - 1][0]) *
                       (samples[index][1] + samples[index - 1][1]) / 2
                       for index in range(1, len(samples)))
        return MemorySummary(
            self.total, self.baseline_used,
            integral / (samples[-1][0] - samples[0][0])
            if samples[-1][0] > samples[0][0] else float(samples[-1][1]),
            max(item[1] for item in samples), integral,
            min(item[2] for item in samples), self._storage_used() - self.storage_used_before,
            max(
                0,
                read_vmstat_counter(self.vmstat, "oom_kill") - self.oom_kill_before,
            ),
        )

    def samples(self) -> list[dict[str, int | float]]:
        with self._lock:
            return [{"monotonic_s": stamp, "used_bytes": used,
                     "available_bytes": available}
                    for stamp, used, available in self._samples]

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            delta, available = self.current()
            with self._lock:
                self._samples.append((time.monotonic(), delta, available))
            if self.sample_hook is not None:
                self.sample_hook(delta, available)

    def _storage_used(self) -> int:
        try:
            usage = shutil.disk_usage(self.storage)
        except OSError:
            return 0
        return usage.total - usage.free


class NumaNodeMemorySampler(NodeMemorySampler):
    """Admission sampler scoped to the configured LOCAL NUMA node."""

    def __init__(self, node: int, *, sys_root: Path = Path("/host/sys"),
                 vmstat: Path = Path("/host/proc/vmstat"),
                 storage: Path = Path("/data/cubelet"), interval_s: float = 0.2) -> None:
        if node < 0:
            raise ValueError("NUMA node must be non-negative")
        host_path = sys_root / "devices/system/node" / f"node{node}" / "meminfo"
        fallback = Path("/sys/devices/system/node") / f"node{node}" / "meminfo"
        self.numa_meminfo = host_path if host_path.exists() else fallback
        if not self.numa_meminfo.exists():
            raise FileNotFoundError(f"NUMA meminfo is unavailable for node {node}")
        self.numa_node = node
        self.meminfo = self.numa_meminfo
        self.vmstat = vmstat if vmstat.exists() else Path("/proc/vmstat")
        self.storage = storage
        self.interval_s = interval_s
        self._stop = Event()
        self._lock = Lock()
        self._samples: list[tuple[float, int, int]] = []
        self._thread = None
        total, free = read_numa_meminfo(self.numa_meminfo)
        self.total = total
        self.baseline_used = total - free
        self.storage_used_before = self._storage_used()
        self.oom_kill_before = read_vmstat_counter(self.vmstat, "oom_kill")

    def current(self) -> tuple[int, int]:
        total, free = read_numa_meminfo(self.numa_meminfo)
        return max(0, total - free - self.baseline_used), free

    def observe(self) -> dict[str, int | str]:
        total, free = read_numa_meminfo(self.numa_meminfo)
        used = max(0, total - free)
        return {
            "metric": "numa_node_memfree",
            "numa_node": self.numa_node,
            "host_mem_total_bytes": total,
            "host_used_bytes": used,
            "host_available_bytes": free,
            "experiment_used_delta_bytes": max(0, used - self.baseline_used),
        }


class CgroupMemorySampler(NodeMemorySampler):
    """Absolute LOCAL charges, including VM overhead and retained page cache.

    Host available memory remains the independent emergency guard. No arm
    baseline is subtracted from the enforced cgroup's admission usage.
    """

    def __init__(self, cgroup: Path, *, capacity_bytes: int,
                 numa_node: int | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cgroup = cgroup
        limit = (cgroup / "memory.max").read_text().strip()
        if limit != str(capacity_bytes):
            raise ValueError(f"LOCAL memory.max {limit} does not match {capacity_bytes}")
        if numa_node is not None:
            nodes = (cgroup / "cpuset.mems.effective").read_text().strip()
            if nodes != str(numa_node):
                raise ValueError(f"LOCAL cpuset {nodes} does not match node {numa_node}")
        self.total = capacity_bytes
        self.baseline_used = 0

    def current(self) -> tuple[int, int]:
        _, available = read_meminfo(self.meminfo)
        return int((self.cgroup / "memory.current").read_text()), available

    def observe(self) -> dict[str, int | str]:
        observation = super().observe()
        used, _ = self.current()
        observation.update({
            "metric": "cgroup_v2_memory_current",
            "local_memory_cgroup": str(self.cgroup),
            "local_capacity_bytes": self.total,
            "local_used_bytes": used,
            "local_memory_events": (self.cgroup / "memory.events").read_text().strip()
            if (self.cgroup / "memory.events").exists() else "unavailable",
            "experiment_used_delta_bytes": used,
        })
        return observation


def sandbox_process_rss_bytes(sandbox_id: str, *,
                              proc_root: Path = Path("/proc")) -> int | None:
    """Sum host RSS for Cube processes whose argv contains ``sandbox_id``."""
    total = 0
    matched = False
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
            if sandbox_id.encode() not in command:
                continue
            for line in (entry / "status").read_text(encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    matched = True
                    break
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            continue
    return total if matched else None


class SandboxRSSSampler:
    """Execution-window host RSS sampler for one CubeSandbox microVM."""

    def __init__(self, sandbox_id: str, *, proc_root: Path = Path("/proc"),
                 interval_s: float = 0.05) -> None:
        self.sandbox_id = sandbox_id
        self.proc_root = proc_root
        self.interval_s = interval_s
        self._stop = Event()
        self._lock = Lock()
        self._samples: list[int] = []
        self._thread: Thread | None = None
        self.baseline_bytes = sandbox_process_rss_bytes(
            sandbox_id, proc_root=proc_root,
        )

    def start(self) -> None:
        self._sample()
        self._thread = Thread(
            target=self._run, name=f"sandbox-rss-{self.sandbox_id[:8]}", daemon=True,
        )
        self._thread.start()

    def stop(self) -> dict[str, int | str | None]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._sample()
        with self._lock:
            samples = tuple(self._samples)
        peak = max(samples) if samples else self.baseline_bytes
        increment = (
            max(0, peak - self.baseline_bytes)
            if peak is not None and self.baseline_bytes is not None else None
        )
        return {
            "metric": "host_proc_vm_rss",
            "host_vm_rss_baseline_bytes": self.baseline_bytes,
            "host_vm_rss_peak_bytes": peak,
            "actual_host_execution_increment_bytes": increment,
            "sample_count": len(samples),
            "validity": "valid" if increment is not None else "unavailable",
        }

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._sample()

    def _sample(self) -> None:
        value = sandbox_process_rss_bytes(
            self.sandbox_id, proc_root=self.proc_root,
        )
        if value is not None:
            with self._lock:
                self._samples.append(value)
