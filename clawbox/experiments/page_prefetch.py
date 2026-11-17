"""Task-independent, GPU-triggered preparation of shared snapshot pages.

Candidates come only from the current immutable RAM lineage. A short batch
collects COLD snapshots that are about to be restored, resolves each GPA to its
newest owning layer, and reads physical ranges in descending shared-benefit per
new byte. Linux still owns page-cache residency and replacement.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from threading import Condition, Event, Lock
import time
from typing import Any, Callable, Mapping

MIB = 1024 * 1024
PAGE = 4096


@dataclass(frozen=True, slots=True)
class FileRange:
    path: Path
    offset: int
    length: int
    layer_index: int


@dataclass(slots=True)
class _BatchMember:
    token: int
    ranges: tuple[FileRange, ...]
    metrics: dict[str, int]
    cancel: Event
    result: dict[str, Any] | None = None


@dataclass(slots=True)
class _Batch:
    batch_id: int
    deadline: float
    members: list[_BatchMember] = field(default_factory=list)
    sealed: bool = False
    leader: int | None = None
    done: bool = False
    error: BaseException | None = None


def _layer_path(snapshot: Path, layer: Mapping[str, Any]) -> Path:
    external = layer.get("external_path")
    if isinstance(external, str) and external:
        return Path(external)
    return Path(str(snapshot) + ".layers") / str(layer["name"])


def _uncovered(start: int, stop: int, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result = []
    cursor = start
    for left, right in covered:
        if right <= cursor:
            continue
        if left >= stop:
            break
        if left > cursor:
            result.append((cursor, min(left, stop)))
        cursor = max(cursor, right)
        if cursor >= stop:
            break
    if cursor < stop:
        result.append((cursor, stop))
    return result


def _cover(covered: list[tuple[int, int]], start: int, stop: int) -> None:
    merged: list[list[int]] = []
    for left, right in sorted((*covered, (start, stop))):
        if not merged or left > merged[-1][1]:
            merged.append([left, right])
        else:
            merged[-1][1] = max(merged[-1][1], right)
    covered[:] = [(left, right) for left, right in merged]


def resolve_lineage_ranges(
    snapshot: Path, *, chunk_bytes: int,
) -> tuple[tuple[FileRange, ...], dict[str, int]]:
    """Return only authoritative physical chunks from the current lineage."""
    sidecar = Path(str(snapshot) + ".lineage.json")
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    logical = document.get("logical_bytes")
    layers = document.get("layers")
    if (document.get("schema_version") not in {1, 2}
            or type(logical) is not int or logical <= 0
            or not isinstance(layers, list) or not layers):
        raise RuntimeError(f"invalid native RAM lineage for page preparation: {sidecar}")
    if chunk_bytes < PAGE or chunk_bytes % PAGE:
        raise ValueError("snapshot page candidate chunk must be 4 KiB aligned")

    covered: list[tuple[int, int]] = []
    authoritative: list[FileRange] = []
    listed_bytes = 0
    stale_bytes_excluded = 0
    for layer_index in range(len(layers) - 1, -1, -1):
        layer = layers[layer_index]
        if not isinstance(layer, Mapping):
            raise RuntimeError(f"invalid lineage layer in {sidecar}")
        path = _layer_path(snapshot, layer)
        stat = path.stat()
        if path.is_symlink() or stat.st_size != logical:
            raise RuntimeError(f"missing or truncated lineage layer for page preparation: {path}")
        owned = []
        for item in layer.get("ranges") or []:
            if not isinstance(item, Mapping):
                raise RuntimeError(f"invalid lineage range in {sidecar}")
            gpa, offset, length = item.get("gpa"), item.get("offset"), item.get("length")
            if (type(gpa) is not int or type(offset) is not int or type(length) is not int
                    or gpa < 0 or offset < 0 or length <= 0
                    or gpa % PAGE or offset % PAGE or length % PAGE
                    # GPA is a guest-physical address, not an offset into the
                    # sparse backing file.  Real Cube snapshots commonly map
                    # a 3 GiB file into guest addresses from 1 GiB through
                    # beyond 4 GiB.  Only the file range is bounded by the
                    # backing file's logical length.
                    or offset + length > logical):
                raise RuntimeError(f"invalid or unaligned lineage range in {sidecar}")
            owned.append((gpa, offset, length))
        for gpa, offset, length in sorted(owned):
            listed_bytes += length
            current = _uncovered(gpa, gpa + length, covered)
            stale_bytes_excluded += length - sum(right - left for left, right in current)
            for left, right in current:
                authoritative.append(FileRange(
                    path, offset + left - gpa, right - left, layer_index,
                ))
            _cover(covered, gpa, gpa + length)

    chunks: dict[tuple[int, int, int, int], FileRange] = {}
    for item in authoritative:
        position = item.offset
        stop = item.offset + item.length
        stat = item.path.stat()
        while position < stop:
            boundary = ((position // chunk_bytes) + 1) * chunk_bytes
            length = min(stop, boundary) - position
            candidate = FileRange(item.path, position, length, item.layer_index)
            chunks.setdefault((stat.st_dev, stat.st_ino, position, length), candidate)
            position += length
    return tuple(chunks.values()), {
        "lineage_listed_bytes": listed_bytes,
        "authoritative_bytes": sum(item.length for item in authoritative),
        "stale_version_bytes_excluded": stale_bytes_excluded,
        "authoritative_fragment_count": len(authoritative),
        "physical_candidate_count": len(chunks),
        "physical_candidate_bytes": sum(item.length for item in chunks.values()),
    }


def _physical_key(item: FileRange) -> tuple[int, int, int, int]:
    stat = item.path.stat()
    return stat.st_dev, stat.st_ino, item.offset, item.length


def _stable_order(key: tuple[int, int, int, int]) -> bytes:
    return hashlib.blake2b(":".join(map(str, key)).encode(), digest_size=16).digest()


def _pread(fd: int, amount: int, offset: int) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(fd, amount, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, amount)


def select_shared_ranges(
    members: Mapping[int, tuple[FileRange, ...]], *, byte_budget: int,
    max_ranges: int,
) -> tuple[list[tuple[FileRange, tuple[int, ...]]], dict[int, dict[str, int]]]:
    """Select a task-neutral physical working set for a waiter batch."""
    candidates: dict[tuple[int, int, int, int], FileRange] = {}
    waiters: dict[tuple[int, int, int, int], set[int]] = {}
    for token, ranges in members.items():
        for item in ranges:
            key = _physical_key(item)
            candidates.setdefault(key, item)
            waiters.setdefault(key, set()).add(token)
    usage = {token: 0 for token in members}
    counts = {token: 0 for token in members}
    selected: list[tuple[FileRange, tuple[int, ...]]] = []
    ordered = sorted(
        candidates,
        key=lambda key: (-len(waiters[key]) / key[3], _stable_order(key)),
    )
    for key in ordered:
        item = candidates[key]
        beneficiaries = tuple(sorted(
            token for token in waiters[key]
            if usage[token] + item.length <= byte_budget and counts[token] < max_ranges
        ))
        if not beneficiaries:
            continue
        selected.append((item, beneficiaries))
        for token in beneficiaries:
            usage[token] += item.length
            counts[token] += 1
        if all(usage[token] >= byte_budget or counts[token] >= max_ranges for token in members):
            break
    maximum = max((len(tokens) for _item, tokens in selected), default=0)
    return selected, {
        token: {
            "selected_bytes": usage[token],
            "selected_range_count": counts[token],
            "max_selected_shared_waiters": maximum,
        }
        for token in members
    }


class SnapshotPagePrefetchController:
    """Arm-wide bounded executor for shared-lineage page preparation."""

    def __init__(self, spec: Any, *, emit: Callable[[Mapping[str, Any]], None]) -> None:
        self.spec = spec
        self.emit = emit
        self._executor = ThreadPoolExecutor(
            max_workers=spec.io_workers, thread_name_prefix="snapshot-page-prefetch",
        )
        self._lock = Lock()
        self._budget = Condition(self._lock)
        self._pending: dict[tuple[int, str], tuple[Event, Future[Any] | None]] = {}
        self._reserved_bytes = 0
        self._inflight_bytes = 0
        self._closed = False
        self._closed_event = Event()
        self._error: BaseException | None = None
        self._rate_lock = Lock()
        self._next_io_time = time.monotonic()
        self._batch_lock = Lock()
        self._batch_ready = Condition(self._batch_lock)
        self._open_batch: _Batch | None = None
        self._next_batch_id = 1
        self._next_member_token = 1

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
                    "session_id": session_id, "case_id": case_id, "role": role,
                    "request_id": request_id, "signal": signal.get("event"),
                    "signal_sequence": signal.get("signal_sequence"),
                    "signal_age_seconds": float(age), "queued_unix_s": time.time(),
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
        with self._batch_ready:
            self._batch_ready.notify_all()
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

    def _reserve_io(self, amount: int) -> bool:
        with self._budget:
            while (not self._closed
                   and self._inflight_bytes + amount > self.spec.max_inflight_mib * MIB):
                self._budget.wait(timeout=0.05)
            if self._closed:
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
        started_mono = time.monotonic()
        started_cpu = time.thread_time()
        event: dict[str, Any] = {
            "event": "snapshot_page_prepare", **context,
            "candidate_source": self.spec.candidate_source,
            "started_unix_s": time.time(), "started_monotonic_s": started_mono,
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
                completed_unix_s=time.time(), completed_monotonic_s=time.monotonic(),
                service_seconds=time.monotonic() - started_mono,
                thread_cpu_seconds=time.thread_time() - started_cpu,
            )
            try:
                self.emit(event)
            finally:
                with self._lock:
                    self._pending.pop(key, None)

    def _join_batch(
        self, ranges: tuple[FileRange, ...], metrics: dict[str, int], cancel: Event,
    ) -> tuple[_Batch, _BatchMember, bool]:
        with self._batch_ready:
            now = time.monotonic()
            if self._open_batch is None or self._open_batch.sealed:
                self._open_batch = _Batch(
                    self._next_batch_id,
                    now + self.spec.batch_window_milliseconds / 1000,
                )
                self._next_batch_id += 1
            batch = self._open_batch
            member = _BatchMember(self._next_member_token, ranges, metrics, cancel)
            self._next_member_token += 1
            batch.members.append(member)
            if len(batch.members) >= self.spec.io_workers:
                batch.sealed = True
                batch.leader = member.token
                self._open_batch = None
                self._batch_ready.notify_all()
            while not batch.sealed:
                remaining = batch.deadline - time.monotonic()
                if remaining <= 0:
                    batch.sealed = True
                    batch.leader = member.token
                    self._open_batch = None
                    self._batch_ready.notify_all()
                    break
                self._batch_ready.wait(timeout=remaining)
            return batch, member, batch.leader == member.token

    def prefetch_snapshot(self, snapshot: Path, role: str, cancel: Event) -> dict[str, Any]:
        del role  # Deliberately independent of task and VM role.
        ranges, metrics = resolve_lineage_ranges(
            snapshot, chunk_bytes=self.spec.candidate_chunk_kib * 1024,
        )
        batch, member, leader = self._join_batch(ranges, metrics, cancel)
        if leader:
            try:
                self._execute_batch(batch)
            except BaseException as exc:
                with self._batch_ready:
                    batch.error = exc
                    batch.done = True
                    self._batch_ready.notify_all()
                raise
        else:
            with self._batch_ready:
                while not batch.done and not cancel.is_set() and not self._closed:
                    self._batch_ready.wait(timeout=0.01)
                if not batch.done and cancel.is_set():
                    return {
                        **metrics, "batch_id": batch.batch_id,
                        "batch_waiter_count": len(batch.members),
                        "selected_bytes": 0, "selected_range_count": 0,
                        "read_bytes": 0, "physical_read_bytes": 0,
                        "cancelled_for_demand": True,
                    }
        if batch.error is not None:
            raise batch.error
        assert member.result is not None
        return member.result

    def _execute_batch(self, batch: _Batch) -> None:
        members_by_token = {member.token: member for member in batch.members}
        active = {
            member.token: member.ranges for member in batch.members
            if not member.cancel.is_set()
        }
        selected, per_member = select_shared_ranges(
            active, byte_budget=self.spec.per_vm_prefetch_mib * MIB,
            max_ranges=self.spec.max_ranges_per_vm,
        )
        read_by_member = {member.token: 0 for member in batch.members}
        ranges_by_member = {member.token: 0 for member in batch.members}
        physical_read_bytes = 0
        physical_read_ranges = 0
        for item, beneficiaries in selected:
            current = tuple(
                token for token in beneficiaries
                if not members_by_token[token].cancel.is_set()
            )
            if not current or self._closed:
                continue
            fd = os.open(item.path, os.O_RDONLY)
            try:
                if hasattr(os, "posix_fadvise"):
                    os.posix_fadvise(
                        fd, item.offset, item.length,
                        getattr(os, "POSIX_FADV_WILLNEED", 3),
                    )
                position = item.offset
                stop = item.offset + item.length
                completed = 0
                while position < stop and not self._closed:
                    amount = min(MIB, stop - position)
                    if not self._throttle(amount) or not self._reserve_io(amount):
                        break
                    try:
                        data = _pread(fd, amount, position)
                    finally:
                        self._release_io(amount)
                    if len(data) != amount:
                        raise RuntimeError(
                            f"short snapshot page preparation read from {item.path}: "
                            f"expected {amount}, got {len(data)}"
                        )
                    physical_read_bytes += len(data)
                    completed += len(data)
                    position += len(data)
                if completed:
                    physical_read_ranges += 1
                    for token in current:
                        read_by_member[token] += completed
                        ranges_by_member[token] += 1
            finally:
                os.close(fd)
        for member in batch.members:
            chosen = per_member.get(member.token, {
                "selected_bytes": 0, "selected_range_count": 0,
                "max_selected_shared_waiters": 0,
            })
            member.result = {
                **member.metrics, **chosen, "batch_id": batch.batch_id,
                "batch_waiter_count": len(batch.members),
                "read_bytes": read_by_member[member.token],
                "read_range_count": ranges_by_member[member.token],
                "physical_read_bytes": (
                    physical_read_bytes if member.token == batch.leader else 0
                ),
                "physical_read_range_count": (
                    physical_read_ranges if member.token == batch.leader else 0
                ),
                "batch_leader": member.token == batch.leader,
                "cancelled_for_demand": member.cancel.is_set(),
            }
        with self._batch_ready:
            batch.done = True
            self._batch_ready.notify_all()

    def _throttle(self, amount: int) -> bool:
        rate = self.spec.prefetch_bandwidth_mib_s * MIB
        with self._rate_lock:
            now = time.monotonic()
            start = max(now, self._next_io_time)
            self._next_io_time = start + amount / rate
            delay = start - now
        return not delay or not self._closed_event.wait(delay)

    def check(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            raise RuntimeError("snapshot page preparation failed") from error

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "candidate_source": self.spec.candidate_source,
                "pending": len(self._pending), "reserved_bytes": self._reserved_bytes,
                "inflight_bytes": self._inflight_bytes,
                "failed": self._error is not None,
            }

    def close(self, *, wait: bool, cancel: bool = False) -> None:
        with self._budget:
            self._closed = True
            self._closed_event.set()
            if cancel:
                for event, _future in self._pending.values():
                    event.set()
            self._budget.notify_all()
        with self._batch_ready:
            if self._open_batch is not None and not self._open_batch.sealed:
                self._open_batch.sealed = True
                if self._open_batch.members:
                    self._open_batch.leader = self._open_batch.members[-1].token
                self._open_batch = None
            self._batch_ready.notify_all()
        self._executor.shutdown(wait=wait, cancel_futures=cancel)
