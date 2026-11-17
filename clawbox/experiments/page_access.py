"""Low-overhead page-idle profiling for a few explicitly selected VMs.

This is intentionally not enabled in formal throughput runs.  Linux's page-idle
bit reports whether a physical page was referenced at least once after the bit
was armed.  Sampling it at a fixed cadence therefore measures access-window
frequency, not memory-instruction counts.
"""

from __future__ import annotations

from array import array
from bisect import bisect_right
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import struct
import sys
from threading import Event, Lock, Thread
import time
from typing import Any, Iterable
import zlib


PAGEMAP_PRESENT = 1 << 63
PAGEMAP_PFN_MASK = (1 << 55) - 1
SAMPLE_HEADER = struct.Struct("<dIII")
SAMPLE_MAGIC = b"CLAWBOX_PAGE_ACCESS_V1\n"


def validate_page_idle_host(
    *, page_idle: Path = Path("/sys/kernel/mm/page_idle/bitmap"),
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    """Fail before VM arrivals when an explicitly requested profile cannot run."""
    if os.name != "posix":
        raise RuntimeError("snapshot page profiling requires Linux")
    if os.geteuid() != 0:
        raise RuntimeError(
            "snapshot page profiling requires root to read PFNs and page-idle bits; "
            "launch with bash scripts/clawbox --sudo"
        )
    if not page_idle.exists():
        raise RuntimeError(
            "snapshot page profiling requires CONFIG_IDLE_PAGE_TRACKING=y and "
            "/sys/kernel/mm/page_idle/bitmap"
        )
    if not os.access(page_idle, os.R_OK | os.W_OK):
        raise RuntimeError(f"snapshot page profiling cannot read/write {page_idle}")
    if not (proc_root / "self" / "pagemap").exists():
        raise RuntimeError("snapshot page profiling requires /proc/<pid>/pagemap")
    return {
        "page_size": os.sysconf("SC_PAGE_SIZE"),
        "page_idle": str(page_idle),
        "proc_root": str(proc_root),
        "metric": "referenced_in_sampling_window",
    }


@dataclass(frozen=True)
class _LineageRange:
    file_offset: int
    end_offset: int
    gpa: int


@dataclass(frozen=True)
class _VirtualRange:
    pid: int
    hva: int
    gpa: int
    length: int
    path: str
    file_offset: int
    layer_index: int
    layer_name: str
    layer_tier: str | None


@dataclass(frozen=True)
class _MapEntry:
    start: int
    end: int
    offset: int
    device: int
    inode: int
    path: str


def _map_entries(path: Path) -> list[_MapEntry]:
    result: list[_MapEntry] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 5:
            continue
        bounds, _perms, offset, device, inode = fields[:5]
        if int(inode) == 0:
            continue
        start, end = (int(value, 16) for value in bounds.split("-", 1))
        major, minor = (int(value, 16) for value in device.split(":", 1))
        result.append(_MapEntry(
            start=start,
            end=end,
            offset=int(offset, 16),
            device=os.makedev(major, minor),
            inode=int(inode),
            path=fields[5].removesuffix(" (deleted)") if len(fields) == 6 else "",
        ))
    return result


def _layer_path(snapshot: Path, layer: dict[str, Any]) -> Path:
    external = layer.get("external_path")
    if isinstance(external, str) and external:
        return Path(external)
    return Path(str(snapshot) + ".layers") / str(layer["name"])


def _path_tier(path: Path, *, warm_root: Path | None, cold_root: Path | None) -> str:
    resolved = path.resolve()
    for name, root in (("warm", warm_root), ("cold", cold_root)):
        if root is not None and resolved.is_relative_to(root.resolve()):
            return name
    return "base"


def _lineage_layout(
    snapshot: Path, *, warm_root: Path | None, cold_root: Path | None,
) -> dict[tuple[int, int], tuple[int, str, str, list[_LineageRange]]]:
    sidecar = Path(str(snapshot) + ".lineage.json")
    try:
        document = json.loads(sidecar.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"snapshot page profiling requires {sidecar}") from exc
    result: dict[tuple[int, int], tuple[int, str, str, list[_LineageRange]]] = {}
    for index, raw_layer in enumerate(document.get("layers") or []):
        if not isinstance(raw_layer, dict):
            continue
        path = _layer_path(snapshot, raw_layer)
        stat = path.stat()
        ranges = sorted((
            _LineageRange(
                file_offset=int(item["offset"]),
                end_offset=int(item["offset"]) + int(item["length"]),
                gpa=int(item["gpa"]),
            )
            for item in raw_layer.get("ranges") or []
        ), key=lambda item: item.file_offset)
        if not ranges:
            continue
        declared_tier = raw_layer.get("tier")
        tier = (declared_tier if isinstance(declared_tier, str)
                else _path_tier(path, warm_root=warm_root, cold_root=cold_root))
        result[(stat.st_dev, stat.st_ino)] = (index, str(raw_layer["name"]), tier, ranges)
    if not result:
        raise RuntimeError(f"snapshot page profiling found no lineage layers in {sidecar}")
    return result


def _intersections(
    entry: _MapEntry,
    *, pid: int,
    layer_index: int,
    layer_name: str,
    layer_tier: str | None,
    ranges: list[_LineageRange],
) -> Iterable[_VirtualRange]:
    file_end = entry.offset + entry.end - entry.start
    starts = [item.file_offset for item in ranges]
    cursor = max(0, bisect_right(starts, entry.offset) - 1)
    while cursor < len(ranges):
        item = ranges[cursor]
        if item.file_offset >= file_end:
            break
        overlap_start = max(entry.offset, item.file_offset)
        overlap_end = min(file_end, item.end_offset)
        if overlap_start < overlap_end:
            yield _VirtualRange(
                pid=pid,
                hva=entry.start + overlap_start - entry.offset,
                gpa=item.gpa + overlap_start - item.file_offset,
                length=overlap_end - overlap_start,
                path=entry.path,
                file_offset=overlap_start,
                layer_index=layer_index,
                layer_name=layer_name,
                layer_tier=layer_tier,
            )
        cursor += 1


def discover_virtual_ranges(
    *, cgroup: Path, snapshot: Path, proc_root: Path = Path("/proc"),
    warm_root: Path | None = None, cold_root: Path | None = None,
) -> tuple[list[_VirtualRange], list[int]]:
    layout = _lineage_layout(snapshot, warm_root=warm_root, cold_root=cold_root)
    try:
        pids = sorted({int(value) for value in (cgroup / "cgroup.procs").read_text().split()})
    except FileNotFoundError as exc:
        raise RuntimeError(f"snapshot page profiling cannot read {cgroup/'cgroup.procs'}") from exc
    found: list[_VirtualRange] = []
    live_pids: list[int] = []
    for pid in pids:
        maps = proc_root / str(pid) / "maps"
        if not maps.exists():
            continue
        matched = False
        for entry in _map_entries(maps):
            layer = layout.get((entry.device, entry.inode))
            if layer is None:
                continue
            layer_index, layer_name, tier, ranges = layer
            selected = list(_intersections(
                entry, pid=pid, layer_index=layer_index,
                layer_name=layer_name, layer_tier=tier, ranges=ranges,
            ))
            if selected:
                matched = True
                found.extend(selected)
        if matched:
            live_pids.append(pid)
    page_size = os.sysconf("SC_PAGE_SIZE")
    if not found:
        raise RuntimeError(
            "snapshot page profiling found no file-backed lineage VMAs; "
            "the restore may have selected copy mode or the VMM process is outside the verified cgroup"
        )
    if any(item.hva % page_size or item.gpa % page_size or item.length % page_size for item in found):
        raise RuntimeError("snapshot page profiling requires page-aligned HVA/GPA lineage ranges")
    return sorted(found, key=lambda item: (item.gpa, item.pid, item.hva)), live_pids


def _runs(values: list[int]) -> Iterable[tuple[int, int]]:
    if not values:
        return
    start = previous = values[0]
    for value in values[1:]:
        if value != previous + 1:
            yield start, previous + 1
            start = value
        previous = value
    yield start, previous + 1


def _coalesce_scan_ranges(ranges: list[_VirtualRange]) -> list[_VirtualRange]:
    """Merge adjacent guest pages so one pagemap pread covers layer boundaries."""
    result: list[_VirtualRange] = []
    for item in sorted(ranges, key=lambda value: (value.pid, value.hva, value.gpa)):
        previous = result[-1] if result else None
        if (previous is not None and previous.pid == item.pid
                and previous.hva + previous.length == item.hva
                and previous.gpa + previous.length == item.gpa):
            result[-1] = _VirtualRange(
                pid=previous.pid, hva=previous.hva, gpa=previous.gpa,
                length=previous.length + item.length, path="", file_offset=0,
                layer_index=-1, layer_name="scan", layer_tier=None,
            )
        else:
            result.append(_VirtualRange(
                pid=item.pid, hva=item.hva, gpa=item.gpa, length=item.length,
                path="", file_offset=0, layer_index=-1,
                layer_name="scan", layer_tier=None,
            ))
    return result


class _PageIdleSegment:
    def __init__(
        self, *, output: Path, ranges: list[_VirtualRange], pids: list[int],
        generation: int, tier: str, interval_s: float, max_windows: int,
        proc_root: Path, page_idle: Path, snapshot: Path,
    ) -> None:
        self.output = output
        self.ranges = ranges
        self.scan_ranges = _coalesce_scan_ranges(ranges)
        self.pids = pids
        self.generation = generation
        self.tier = tier
        self.interval_s = interval_s
        self.max_windows = max_windows
        self.proc_root = proc_root
        self.page_idle = page_idle
        self.snapshot = snapshot
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        self.page_count = max((item.gpa + item.length) // self.page_size for item in ranges)
        self.access_counts = array("H", [0]) * self.page_count
        self.present_counts = array("H", [0]) * self.page_count
        self._previous_pfns = array("Q", [0]) * self.page_count
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._error: BaseException | None = None
        self._windows = 0
        self._pfn_changes = 0
        self._started_wall = time.time()
        self._started_mono = time.monotonic()
        self._sample_cpu_seconds = 0.0
        self._samples = None

    def _scan_pagemaps(self) -> list[tuple[int, int]]:
        records: list[tuple[int, int]] = []
        descriptors: dict[int, int] = {}
        try:
            for item in self.scan_ranges:
                fd = descriptors.get(item.pid)
                if fd is None:
                    fd = os.open(self.proc_root / str(item.pid) / "pagemap", os.O_RDONLY)
                    descriptors[item.pid] = fd
                pages = item.length // self.page_size
                data = os.pread(fd, pages * 8, item.hva // self.page_size * 8)
                if len(data) != pages * 8:
                    raise RuntimeError(f"short pagemap read for pid {item.pid}")
                entries = array("Q")
                entries.frombytes(data)
                if sys.byteorder != "little":
                    entries.byteswap()
                logical = item.gpa // self.page_size
                for offset, value in enumerate(entries):
                    if value & PAGEMAP_PRESENT:
                        pfn = value & PAGEMAP_PFN_MASK
                        if pfn:
                            records.append((logical + offset, pfn))
        finally:
            for fd in descriptors.values():
                os.close(fd)
        return records

    @staticmethod
    def _word_masks(records: Iterable[tuple[int, int]]) -> dict[int, int]:
        words: dict[int, int] = {}
        for _logical, pfn in records:
            word = pfn // 64
            words[word] = words.get(word, 0) | (1 << (pfn % 64))
        return words

    @staticmethod
    def _read_words(fd: int, words: list[int]) -> dict[int, int]:
        result: dict[int, int] = {}
        for start, end in _runs(words):
            data = os.pread(fd, (end - start) * 8, start * 8)
            if len(data) != (end - start) * 8:
                raise RuntimeError("short page-idle bitmap read")
            for offset, (value,) in enumerate(struct.iter_unpack("<Q", data)):
                result[start + offset] = value
        return result

    @staticmethod
    def _arm_words(fd: int, masks: dict[int, int]) -> None:
        words = sorted(masks)
        for start, end in _runs(words):
            data = b"".join(struct.pack("<Q", masks.get(word, 0)) for word in range(start, end))
            written = os.pwrite(fd, data, start * 8)
            if written != len(data):
                raise RuntimeError("short page-idle bitmap write")

    def arm(self) -> int:
        records = self._scan_pagemaps()
        with os.fdopen(os.open(self.page_idle, os.O_RDWR), "r+b", buffering=0) as stream:
            self._arm_words(stream.fileno(), self._word_masks(records))
        for logical, pfn in records:
            self._previous_pfns[logical] = pfn
        return len(records)

    def sample(self) -> dict[str, int | float]:
        cpu_started = time.thread_time()
        records = self._scan_pagemaps()
        masks = self._word_masks(records)
        words = sorted(masks)
        fd = os.open(self.page_idle, os.O_RDWR)
        try:
            idle = self._read_words(fd, words)
            accessed = 0
            pfn_changes = 0
            for logical, pfn in records:
                previous = self._previous_pfns[logical]
                changed = previous not in (0, pfn)
                active = changed or not (idle.get(pfn // 64, 0) & (1 << (pfn % 64)))
                self.present_counts[logical] += 1
                if active:
                    self.access_counts[logical] += 1
                    accessed += 1
                if changed:
                    pfn_changes += 1
                self._previous_pfns[logical] = pfn
            self._arm_words(fd, masks)
        finally:
            os.close(fd)
        elapsed = time.monotonic() - self._started_mono
        self._samples.write(SAMPLE_HEADER.pack(
            elapsed, len(records), accessed, pfn_changes,
        ))
        self._samples.flush()
        self._windows += 1
        self._pfn_changes += pfn_changes
        self._sample_cpu_seconds += time.thread_time() - cpu_started
        return {
            "present_pages": len(records),
            "accessed_pages": accessed,
            "pfn_changes": pfn_changes,
            "window": self._windows,
            "elapsed_seconds": elapsed,
        }

    def _run(self) -> None:
        deadline = time.monotonic() + self.interval_s
        try:
            while not self._stop.wait(max(0.0, deadline - time.monotonic())):
                if self._windows >= self.max_windows:
                    break
                self.sample()
                deadline += self.interval_s
        except BaseException as exc:
            self._error = exc

    def start(self) -> None:
        self.output.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(str(self.snapshot) + ".lineage.json", self.output / "lineage.json")
        maps = []
        for pid in self.pids:
            source = self.proc_root / str(pid) / "maps"
            text = source.read_text(encoding="utf-8", errors="replace")
            (self.output / f"proc-{pid}-maps.txt").write_text(text, encoding="utf-8")
            maps.append({"pid": pid, "path": str(source)})
        (self.output / "ranges.json").write_text(json.dumps({
            "schema_version": 1,
            "generation": self.generation,
            "tier": self.tier,
            "page_size": self.page_size,
            "page_count": self.page_count,
            "scan_range_count": len(self.scan_ranges),
            "snapshot": str(self.snapshot),
            "pids": self.pids,
            "ranges": [item.__dict__ for item in self.ranges],
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        self._samples = (self.output / "samples.bin").open("wb", buffering=0)
        self._samples.write(SAMPLE_MAGIC)
        present = self.arm()
        (self.output / "started.json").write_text(json.dumps({
            "schema_version": 1,
            "started_unix_s": self._started_wall,
            "metric": "referenced_in_sampling_window",
            "initial_present_pages": present,
            "interval_seconds": self.interval_s,
            "max_windows": self.max_windows,
            "sample_record": "little-endian <elapsed_f64,present_u32,accessed_u32,pfn_changes_u32>",
            "maps": maps,
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        self._thread = Thread(
            target=self._run,
            name=f"page-idle-g{self.generation}", daemon=True,
        )
        self._thread.start()

    def stop(self, *, reason: str) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        try:
            if self._error is not None:
                raise RuntimeError("snapshot page profile sampling failed") from self._error
            if self._windows < self.max_windows:
                self.sample()
            access_raw = self.access_counts.tobytes()
            present_raw = self.present_counts.tobytes()
            (self.output / "access-counts.u16.zlib").write_bytes(zlib.compress(access_raw, 6))
            (self.output / "present-counts.u16.zlib").write_bytes(zlib.compress(present_raw, 6))
            summary = {
                "schema_version": 1,
                "metric": "referenced_in_sampling_window",
                "generation": self.generation,
                "tier": self.tier,
                "reason": reason,
                "windows": self._windows,
                "duration_seconds": time.monotonic() - self._started_mono,
                "interval_seconds": self.interval_s,
                "page_size": self.page_size,
                "page_count": self.page_count,
                "counts_dtype": "uint16-little-endian",
                "pages_ever_present": sum(1 for value in self.present_counts if value),
                "pages_ever_accessed": sum(1 for value in self.access_counts if value),
                "accessed_page_windows": sum(self.access_counts),
                "present_page_windows": sum(self.present_counts),
                "pfn_changes": self._pfn_changes,
                "sampler_thread_cpu_seconds": self._sample_cpu_seconds,
                "stopped_unix_s": time.time(),
            }
            (self.output / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            )
            return summary
        finally:
            if self._samples is not None:
                self._samples.close()
                self._samples = None


class SnapshotPageProfiler:
    """Own the sequence of live intervals for one session role."""

    def __init__(
        self, *, output: Path, session_id: str, case_id: str, role: str,
        interval_s: float, max_windows: int,
        warm_root: Path | None = None, cold_root: Path | None = None,
        proc_root: Path = Path("/proc"),
        page_idle: Path = Path("/sys/kernel/mm/page_idle/bitmap"),
    ) -> None:
        self.output = output
        self.session_id = session_id
        self.case_id = case_id
        self.role = role
        self.interval_s = interval_s
        self.max_windows = max_windows
        self.warm_root = warm_root
        self.cold_root = cold_root
        self.proc_root = proc_root
        self.page_idle = page_idle
        self._segment: _PageIdleSegment | None = None
        self._sequence = 0

    def start(
        self, *, cgroup: Path, generation: int, tier: str,
        snapshot_path: str,
    ) -> dict[str, Any]:
        if self._segment is not None:
            raise RuntimeError("snapshot page profiler already has an active generation")
        snapshot = Path(snapshot_path)
        ranges, pids = discover_virtual_ranges(
            cgroup=cgroup, snapshot=snapshot, proc_root=self.proc_root,
            warm_root=self.warm_root, cold_root=self.cold_root,
        )
        self._sequence += 1
        segment_output = self.output / f"g{generation}-{self._sequence:02d}"
        segment = _PageIdleSegment(
            output=segment_output, ranges=ranges, pids=pids,
            generation=generation, tier=tier,
            interval_s=self.interval_s, max_windows=self.max_windows,
            proc_root=self.proc_root, page_idle=self.page_idle,
            snapshot=snapshot,
        )
        segment.start()
        self._segment = segment
        return {
            "event": "snapshot_page_profile_started",
            "session_id": self.session_id,
            "case_id": self.case_id,
            "role": self.role,
            "generation": generation,
            "tier": tier,
            "pids": pids,
            "range_count": len(ranges),
            "artifact": str(segment_output),
        }

    def stop(self, *, reason: str) -> dict[str, Any] | None:
        segment, self._segment = self._segment, None
        if segment is None:
            return None
        return {
            "event": "snapshot_page_profile_stopped",
            "session_id": self.session_id,
            "case_id": self.case_id,
            "role": self.role,
            "artifact": str(segment.output),
            **segment.stop(reason=reason),
        }
