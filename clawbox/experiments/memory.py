from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread


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
        self._thread = Thread(target=self._run, name="node-memory-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> MemorySummary:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            samples = list(self._samples)
        if not samples:
            delta, available = self.current()
            samples = [(time.monotonic(), delta, available)]
        integral = sum((samples[index][0] - samples[index - 1][0]) *
                       (samples[index][1] + samples[index - 1][1]) / 2
                       for index in range(1, len(samples)))
        return MemorySummary(
            self.total, self.baseline_used,
            sum(item[1] for item in samples) / len(samples),
            max(item[1] for item in samples), integral,
            min(item[2] for item in samples), self._storage_used() - self.storage_used_before,
            max(
                0,
                read_vmstat_counter(self.vmstat, "oom_kill") - self.oom_kill_before,
            ),
        )

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            delta, available = self.current()
            with self._lock:
                self._samples.append((time.monotonic(), delta, available))

    def _storage_used(self) -> int:
        try:
            usage = shutil.disk_usage(self.storage)
        except OSError:
            return 0
        return usage.total - usage.free
