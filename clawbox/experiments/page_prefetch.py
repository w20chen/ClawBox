"""Bounded GPU-triggered prefetch of profiled snapshot GPA ranges.

The hotset contains only guest-physical ranges.  At preparation time those
ranges are resolved through the current native lineage, so the read always
targets the authoritative inode and offset for that snapshot generation.
Linux remains responsible for page-cache sharing and replacement.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from threading import Condition, Event, Lock
import time
from typing import Any, Callable, Mapping


MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class HotRange:
    gpa: int
    length: int
    score: float


@dataclass(frozen=True, slots=True)
class FileRange:
    path: Path
    offset: int
    length: int
    score: float
    layer_index: int


def _load_hotset(path: Path) -> tuple[dict[str, tuple[HotRange, ...]], str]:
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid snapshot hotset manifest {path}: {exc}") from exc
    if document.get("schema_version") != 1 or document.get("page_size") != 4096:
        raise ValueError("snapshot hotset manifest requires schema_version=1 and page_size=4096")
    roles = document.get("roles")
    if not isinstance(roles, Mapping) or set(roles) != {"runtime", "tool"}:
        raise ValueError("snapshot hotset manifest requires exactly runtime and tool roles")
    result: dict[str, tuple[HotRange, ...]] = {}
    for role, values in roles.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"snapshot hotset role {role} must contain ranges")
        ranges = []
        for value in values:
            if not isinstance(value, Mapping):
                raise ValueError(f"snapshot hotset role {role} contains a non-object range")
            gpa, length, score = value.get("gpa"), value.get("length"), value.get("score")
            if (type(gpa) is not int or type(length) is not int
                    or gpa < 0 or length <= 0 or gpa % 4096 or length % 4096
                    or isinstance(score, bool) or not isinstance(score, (int, float))
                    or not 0 <= float(score) <= 1):
                raise ValueError(f"snapshot hotset role {role} contains an invalid range")
            ranges.append(HotRange(gpa, length, float(score)))
        result[str(role)] = tuple(sorted(ranges, key=lambda item: (-item.score, item.gpa)))
    return result, hashlib.sha256(raw).hexdigest()


def _layer_path(snapshot: Path, layer: Mapping[str, Any]) -> Path:
    external = layer.get("external_path")
    if isinstance(external, str) and external:
        return Path(external)
    return Path(str(snapshot) + ".layers") / str(layer["name"])


def resolve_hot_ranges(
    snapshot: Path, hot_ranges: tuple[HotRange, ...], *,
    byte_budget: int, max_ranges: int,
) -> tuple[list[FileRange], dict[str, int]]:
    """Resolve immutable GPA candidates to current lineage file ranges."""
    sidecar = Path(str(snapshot) + ".lineage.json")
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    if document.get("schema_version") not in {1, 2}:
        raise RuntimeError(f"invalid native RAM lineage for page prefetch: {sidecar}")
    fragments: list[FileRange] = []
    candidate_bytes = sum(item.length for item in hot_ranges)
    for layer_index, layer in enumerate(document.get("layers") or []):
        if not isinstance(layer, Mapping):
            continue
        path = _layer_path(snapshot, layer)
        stat = path.stat()
        logical = int(document.get("logical_bytes") or 0)
        if path.is_symlink() or logical <= 0 or stat.st_size != logical:
            raise RuntimeError(f"missing or truncated lineage layer for page prefetch: {path}")
        owned = sorted(
            (
                int(item["gpa"]), int(item["offset"]), int(item["length"])
            )
            for item in layer.get("ranges") or []
            if isinstance(item, Mapping)
        )
        for hot in hot_ranges:
            hot_end = hot.gpa + hot.length
            for gpa, offset, length in owned:
                end = gpa + length
                if end <= hot.gpa:
                    continue
                if gpa >= hot_end:
                    break
                start = max(gpa, hot.gpa)
                stop = min(end, hot_end)
                if start < stop:
                    fragments.append(FileRange(
                        path=path,
                        offset=offset + start - gpa,
                        length=stop - start,
                        score=hot.score,
                        layer_index=layer_index,
                    ))

    # One inode can have several hardlink names.  Keep one canonical read for
    # each physical file range, then merge adjacent selected ranges.
    unique: dict[tuple[int, int, int, int], FileRange] = {}
    for item in fragments:
        stat = item.path.stat()
        key = (stat.st_dev, stat.st_ino, item.offset, item.length)
        previous = unique.get(key)
        if previous is None or item.score > previous.score:
            unique[key] = item
    selected: list[FileRange] = []
    remaining = byte_budget
    for item in sorted(unique.values(), key=lambda value: (-value.score, -value.length, value.offset)):
        if remaining <= 0:
            break
        length = min(item.length, remaining)
        length -= length % 4096
        if length:
            selected.append(FileRange(
                item.path, item.offset, length, item.score, item.layer_index,
            ))
            remaining -= length

    by_inode: dict[tuple[int, int], list[FileRange]] = {}
    for item in selected:
        stat = item.path.stat()
        by_inode.setdefault((stat.st_dev, stat.st_ino), []).append(item)
    merged: list[FileRange] = []
    for values in by_inode.values():
        values.sort(key=lambda item: item.offset)
        current = values[0]
        for item in values[1:]:
            current_end = current.offset + current.length
            if item.offset <= current_end:
                end = max(current_end, item.offset + item.length)
                current = FileRange(
                    current.path, current.offset, end - current.offset,
                    max(current.score, item.score), current.layer_index,
                )
            else:
                merged.append(current)
                current = item
        merged.append(current)
    merged.sort(key=lambda item: (-item.score, -item.length, str(item.path), item.offset))
    if len(merged) > max_ranges:
        merged = merged[:max_ranges]
    return merged, {
        "hotset_candidate_bytes": candidate_bytes,
        "resolved_fragment_count": len(fragments),
        "deduplicated_fragment_count": len(unique),
        "selected_range_count": len(merged),
        "selected_bytes": sum(item.length for item in merged),
    }


class SnapshotPagePrefetchController:
    """Arm-wide bounded I/O executor used only by the enabled research arm."""

    def __init__(self, spec: Any, *, emit: Callable[[Mapping[str, Any]], None]) -> None:
        self.spec = spec
        self.emit = emit
        self.hotsets, self.manifest_sha256 = _load_hotset(Path(spec.hotset_manifest))
        self._executor = ThreadPoolExecutor(
            max_workers=spec.io_workers, thread_name_prefix="snapshot-page-prefetch",
        )
        self._lock = Lock()
        self._budget = Condition(self._lock)
        self._pending: dict[tuple[int, str], tuple[Event, Future[Any] | None]] = {}
        self._reserved_bytes = 0
        self._inflight_bytes = 0
        self._closed = False
        self._error: BaseException | None = None
        self._rate_lock = Lock()
        self._next_io_time = time.monotonic()

    def submit(
        self, lifecycle: Any, *, session_id: str, case_id: str, role: str,
        request_id: str, signal: Mapping[str, Any],
    ) -> bool:
        age = signal.get("signal_age_seconds", 0.0)
        if not isinstance(age, (int, float)) or age < 0 or age > self.spec.signal_max_age_seconds:
            return False
        key = (id(lifecycle), request_id)
        cancel = Event()
        with self._lock:
            if self._closed or key in self._pending:
                return False
            self._pending[key] = (cancel, None)
            future = self._executor.submit(
                self._run, key, lifecycle, cancel,
                {
                    "session_id": session_id,
                    "case_id": case_id,
                    "role": role,
                    "request_id": request_id,
                    "signal": signal.get("event"),
                    "signal_sequence": signal.get("signal_sequence"),
                    "signal_age_seconds": float(age),
                    "queued_unix_s": time.time(),
                    "queued_monotonic_s": time.monotonic(),
                },
            )
            self._pending[key] = (cancel, future)
        return True

    def cancel(self, lifecycle: Any, request_id: str) -> bool:
        with self._lock:
            pending = self._pending.get((id(lifecycle), request_id))
            if pending is None:
                return False
            pending[0].set()
            return True

    def _reserve_working_set(self, amount: int, cancel: Event) -> bool:
        with self._budget:
            while (not self._closed and not cancel.is_set()
                   and self._reserved_bytes + amount > self.spec.prefetch_budget_mib * MIB):
                self._budget.wait(timeout=0.05)
            if self._closed or cancel.is_set():
                return False
            self._reserved_bytes += amount
            return True

    def _release_working_set(self, amount: int) -> None:
        with self._budget:
            self._reserved_bytes -= amount
            self._budget.notify_all()

    def _reserve_io(self, amount: int, cancel: Event) -> bool:
        with self._budget:
            while (not self._closed and not cancel.is_set()
                   and self._inflight_bytes + amount > self.spec.max_inflight_mib * MIB):
                self._budget.wait(timeout=0.05)
            if self._closed or cancel.is_set():
                return False
            self._inflight_bytes += amount
            return True

    def _release_io(self, amount: int) -> None:
        with self._budget:
            self._inflight_bytes -= amount
            self._budget.notify_all()

    def _run(
        self, key: tuple[int, str], lifecycle: Any, cancel: Event,
        context: dict[str, Any],
    ) -> None:
        reservation = self.spec.per_vm_prefetch_mib * MIB
        started_wall = time.time()
        started_mono = time.monotonic()
        started_cpu = time.thread_time()
        event: dict[str, Any] = {
            "event": "snapshot_page_prepare",
            **context,
            "manifest_sha256": self.manifest_sha256,
            "started_unix_s": started_wall,
            "started_monotonic_s": started_mono,
        }
        reserved = False
        try:
            reserved = self._reserve_working_set(reservation, cancel)
            if not reserved:
                event.update(status="cancelled", reason="cancelled_before_budget")
                return
            event.update(lifecycle.prepare_snapshot_pages(
                controller=self, cancel=cancel, context=context,
            ))
            event.setdefault("status", "ok")
        except BaseException as exc:
            event.update(status="error", error_type=type(exc).__name__, error=str(exc))
            with self._lock:
                if self._error is None:
                    self._error = exc
        finally:
            if reserved:
                self._release_working_set(reservation)
            event.update(
                completed_unix_s=time.time(),
                completed_monotonic_s=time.monotonic(),
                service_seconds=time.monotonic() - started_mono,
                thread_cpu_seconds=time.thread_time() - started_cpu,
            )
            try:
                self.emit(event)
            finally:
                with self._lock:
                    self._pending.pop(key, None)

    def prefetch_snapshot(self, snapshot: Path, role: str, cancel: Event) -> dict[str, Any]:
        ranges, metrics = resolve_hot_ranges(
            snapshot, self.hotsets[role],
            byte_budget=self.spec.per_vm_prefetch_mib * MIB,
            max_ranges=self.spec.max_ranges_per_vm,
        )
        read_bytes = 0
        read_ranges = 0
        cancelled = False
        for item in ranges:
            if cancel.is_set():
                cancelled = True
                break
            fd = os.open(item.path, os.O_RDONLY)
            try:
                if hasattr(os, "posix_fadvise"):
                    os.posix_fadvise(
                        fd, item.offset, item.length,
                        getattr(os, "POSIX_FADV_WILLNEED", 3),
                    )
                position = item.offset
                stop = item.offset + item.length
                while position < stop:
                    if cancel.is_set():
                        cancelled = True
                        break
                    amount = min(MIB, stop - position)
                    if not self._throttle(amount, cancel):
                        cancelled = True
                        break
                    if not self._reserve_io(amount, cancel):
                        cancelled = True
                        break
                    try:
                        data = os.pread(fd, amount, position)
                    finally:
                        self._release_io(amount)
                    if len(data) != amount:
                        raise RuntimeError(
                            f"short snapshot page prefetch read from {item.path}: "
                            f"expected {amount}, got {len(data)}"
                        )
                    read_bytes += len(data)
                    position += len(data)
                read_ranges += int(position > item.offset)
            finally:
                os.close(fd)
            if cancelled:
                break
        return {
            **metrics,
            "read_bytes": read_bytes,
            "read_range_count": read_ranges,
            "cancelled_for_demand": cancelled,
        }

    def _throttle(self, amount: int, cancel: Event) -> bool:
        rate = self.spec.prefetch_bandwidth_mib_s * MIB
        with self._rate_lock:
            now = time.monotonic()
            start = max(now, self._next_io_time)
            self._next_io_time = start + amount / rate
            delay = start - now
        return not delay or not cancel.wait(delay)

    def check(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            raise RuntimeError("snapshot page preparation failed") from error

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "manifest_sha256": self.manifest_sha256,
                "pending": len(self._pending),
                "reserved_bytes": self._reserved_bytes,
                "inflight_bytes": self._inflight_bytes,
                "failed": self._error is not None,
            }

    def close(self, *, wait: bool, cancel: bool = False) -> None:
        with self._budget:
            self._closed = True
            if cancel:
                for event, _future in self._pending.values():
                    event.set()
            self._budget.notify_all()
        self._executor.shutdown(wait=wait, cancel_futures=cancel)
