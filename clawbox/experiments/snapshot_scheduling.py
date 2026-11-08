"""BPO-style snapshot branching across two logical storage/compute nodes.

CubeSandbox can create a new sandbox from an application snapshot, but its
public API cannot restore from caller-supplied copied rootfs/memory paths.
Accordingly, cross-node trials perform and verify either a shared-storage relay
or a direct cross-XFS copy, then create the branch through Cube's native
snapshot template API. Results retain the native restore and copied-data
timings separately.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from clawbox.cube.client import CubeSandboxClient, Ownership

from .priority import PriorityDomain
from .results import FailureCategory, ResultEnvelope, RunStatus, utcnow
from .spec import ExperimentArm, SnapshotBranchingSpec, SnapshotSchedulingStorageSpec

MIB = 1024 ** 2


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _distribution(values: list[float]) -> dict[str, float | None]:
    return {
        "avg": statistics.fmean(values) if values else None,
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
    }


def _allocated(path: Path) -> int:
    if path.is_file():
        stat = path.stat()
        return getattr(stat, "st_blocks", (stat.st_size + 511) // 512) * 512
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            stat = item.stat()
            total += getattr(stat, "st_blocks", (stat.st_size + 511) // 512) * 512
    return total


def _digest(path: Path) -> str:
    """Hash logical size, data-extent offsets and bytes without reading holes."""
    value = hashlib.sha256()
    size = path.stat().st_size
    value.update(size.to_bytes(8, "big"))
    descriptor = os.open(path, os.O_RDONLY)
    try:
        offset = 0
        while offset < size:
            if not hasattr(os, "SEEK_DATA"):
                data = offset
            else:
                try:
                    data = os.lseek(descriptor, offset, os.SEEK_DATA)
                except OSError:
                    data = offset
            if data >= size:
                break
            if not hasattr(os, "SEEK_HOLE"):
                hole = size
            else:
                try:
                    hole = min(size, os.lseek(descriptor, data, os.SEEK_HOLE))
                except OSError:
                    hole = size
            value.update(data.to_bytes(8, "big"))
            os.lseek(descriptor, data, os.SEEK_SET)
            remaining = hole - data
            while remaining:
                chunk = os.read(descriptor, min(4 * MIB, remaining))
                if not chunk:
                    raise RuntimeError(f"short read while hashing {path}")
                value.update(chunk)
                remaining -= len(chunk)
            offset = hole
    finally:
        os.close(descriptor)
    return value.hexdigest()


def _copy_measured(source: Path, target: Path) -> tuple[int, float]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    started = time.monotonic()
    copied = 0
    try:
        source_fd = os.open(source, os.O_RDONLY)
        target_fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            size = os.fstat(source_fd).st_size
            os.ftruncate(target_fd, size)
            offset = 0
            while offset < size:
                if not hasattr(os, "SEEK_DATA"):
                    data = offset
                else:
                    try:
                        data = os.lseek(source_fd, offset, os.SEEK_DATA)
                    except OSError:
                        data = offset
                if data >= size:
                    break
                if not hasattr(os, "SEEK_HOLE"):
                    hole = size
                else:
                    try:
                        hole = min(size, os.lseek(source_fd, data, os.SEEK_HOLE))
                    except OSError:
                        hole = size
                os.lseek(source_fd, data, os.SEEK_SET)
                os.lseek(target_fd, data, os.SEEK_SET)
                remaining = hole - data
                while remaining:
                    chunk = os.read(source_fd, min(4 * MIB, remaining))
                    if not chunk:
                        raise RuntimeError(f"short read while copying {source}")
                    view = memoryview(chunk)
                    while view:
                        written = os.write(target_fd, view)
                        view = view[written:]
                    copied += len(chunk)
                    remaining -= len(chunk)
                offset = hole
            os.fsync(target_fd)
        finally:
            os.close(source_fd)
            os.close(target_fd)
        os.replace(temporary, target)
        try:
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            # Windows cannot open/fsync a directory; the target file itself was
            # already fsynced before the atomic replacement.
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return copied, time.monotonic() - started


def _safe_arm_directory(
    root: Path, run_id: str, arm_id: str, task_uid: str,
) -> Path:
    directory = (root / "clawbox-snapshot-scheduling" / run_id / arm_id).resolve()
    resolved_root = root.resolve()
    if directory == resolved_root or not directory.is_relative_to(resolved_root):
        raise ValueError(f"arm storage escaped configured root: {directory}")
    directory.mkdir(parents=True, exist_ok=False)
    marker = directory / ".clawbox-owner.json"
    marker.write_text(json.dumps({
        "task_uid": task_uid, "run_id": run_id, "arm_id": arm_id,
    }), encoding="utf-8")
    return directory


def _shared_arm_directories(
    roots: dict[str, str], run_id: str, arm_id: str, task_uid: str,
) -> dict[str, Path]:
    """Create one NFS directory and prove both logical-node views alias it."""
    result: dict[str, Path] = {}
    first: Path | None = None
    for node, raw_root in roots.items():
        root = Path(raw_root)
        if first is None:
            directory = _safe_arm_directory(root, run_id, arm_id, task_uid)
            first = directory
        else:
            directory = (
                root / "clawbox-snapshot-scheduling" / run_id / arm_id
            ).resolve()
            resolved_root = root.resolve()
            if directory == resolved_root or not directory.is_relative_to(resolved_root):
                raise ValueError(f"shared arm storage escaped configured root: {directory}")
            marker = directory / ".clawbox-owner.json"
            try:
                owner = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"shared NFS view did not expose the owned arm directory: {directory}"
                ) from exc
            if owner.get("task_uid") != task_uid:
                raise RuntimeError(
                    "logical-node shared mounts do not expose the same owned NFS directory"
                )
        result[node] = directory
    return result


def validate_applied_storage(spec: SnapshotSchedulingStorageSpec) -> dict[str, Any]:
    """Fail before VM creation unless the reviewed XFS/NFS/tmpfs plan is live."""
    if spec.topology == "peer-local-ssd":
        from .snapshot_host import validate_peer_storage
        return validate_peer_storage(spec)
    receipt_path = Path(spec.host_receipt)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"snapshot storage receipt is unavailable: {receipt_path}; "
            "run experiment snapshot-storage check/apply"
        ) from exc
    if receipt.get("status") != "applied":
        raise RuntimeError(f"snapshot storage receipt is not applied: {receipt_path}")

    def mount(path: str) -> dict[str, str]:
        completed = subprocess.run(
            ["findmnt", "-J", "-T", path, "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
            check=True, text=True, capture_output=True,
        )
        filesystems = json.loads(completed.stdout).get("filesystems") or []
        if len(filesystems) != 1:
            raise RuntimeError(f"cannot resolve one filesystem for {path}")
        return filesystems[0]

    local = {node: mount(path) for node, path in spec.local_roots.items()}
    if any(item["fstype"] != "xfs" for item in local.values()):
        raise RuntimeError("logical node local roots must be XFS mounts")
    if len({item["source"] for item in local.values()}) != 2:
        raise RuntimeError("logical node local roots do not use independent filesystems")
    for node, path in spec.local_roots.items():
        total = shutil.disk_usage(path).total
        requested = spec.local_capacity_mib * MIB
        if total < requested * 0.95 or total > requested * 1.05:
            raise RuntimeError(
                f"{node} local XFS capacity differs from configured equal quota: "
                f"actual={total}, configured={requested}"
            )
    shared = {node: mount(path) for node, path in spec.shared_mounts.items()}
    if any(item["fstype"] not in {"nfs", "nfs4"} for item in shared.values()):
        raise RuntimeError("shared SSD paths must be NFS mounts")
    exports = {
        item["source"].split(":", 1)[1]
        if ":" in item["source"] else item["source"]
        for item in shared.values()
    }
    if len(exports) != 1:
        raise RuntimeError("logical nodes must mount the same NFS shared SSD export")
    warm = mount(spec.warm_root)
    if warm["fstype"] != "tmpfs":
        raise RuntimeError("shared DRAM snapshot cache must be a tmpfs mount")
    if shutil.disk_usage(spec.warm_root).total < spec.warm_capacity_mib * MIB * 0.95:
        raise RuntimeError("shared DRAM mount is smaller than the configured capacity")
    expected = {
        "local_roots": spec.local_roots,
        "shared_mounts": spec.shared_mounts,
        "warm_root": spec.warm_root,
        "local_capacity_mib": spec.local_capacity_mib,
        "shared_capacity_mib": spec.shared_capacity_mib,
        "warm_capacity_mib": spec.warm_capacity_mib,
    }
    if receipt.get("storage") != expected:
        raise RuntimeError(
            "snapshot storage receipt does not match this experiment; rerun host apply"
        )
    return {"receipt": str(receipt_path), "local": local, "shared": shared, "warm": warm}


def _remove_owned_directory(path: Path, *, root: Path, task_uid: str) -> None:
    resolved = path.resolve()
    root = root.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise RuntimeError(f"refusing to remove path outside configured root: {resolved}")
    marker = resolved / ".clawbox-owner.json"
    try:
        owner = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"refusing to remove unowned arm storage: {resolved}") from exc
    if owner.get("task_uid") != task_uid:
        raise RuntimeError(f"refusing to remove storage owned by another task: {resolved}")
    shutil.rmtree(resolved)


def cleanup_snapshot_run_storage(
    spec: SnapshotSchedulingStorageSpec, *, run_id: str,
    arms: dict[str, dict[str, Any]], cold_snapshot_roots: dict[str, str] | None = None,
) -> list[str]:
    """Remove only run-scoped scenario storage whose task marker still matches."""
    roots = [Path(path) for path in spec.local_roots.values()]
    if spec.shared_mounts:
        roots.append(Path(next(iter(spec.shared_mounts.values()))))
    roots.append(Path(spec.warm_root))
    for value in (cold_snapshot_roots or {}).values():
        root = Path(value)
        if root not in roots:
            roots.append(root)
    cleaned: list[str] = []
    failures: list[str] = []
    for arm_id, arm in arms.items():
        task_uid = arm.get("task_uid")
        if not task_uid:
            continue
        removed = False
        for root in roots:
            directory = root / "clawbox-snapshot-scheduling" / run_id / arm_id
            if not directory.exists():
                continue
            try:
                _remove_owned_directory(directory, root=root, task_uid=str(task_uid))
                removed = True
            except Exception as exc:
                failures.append(
                    f"{arm_id} at {directory}: {type(exc).__name__}: {exc}"
                )
        if removed:
            cleaned.append(arm_id)
    for root in roots:
        run_root = root / "clawbox-snapshot-scheduling" / run_id
        try:
            run_root.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # A nonempty run directory is either reported above or belongs to
            # a task absent from this immutable run state; never remove it broadly.
            pass
    if failures:
        raise RuntimeError("; ".join(failures))
    return cleaned


@dataclass(frozen=True)
class ParentPackage:
    parent_id: str
    source_node: str
    snapshot_id: str
    version: int
    directory: Path
    rootfs_files: tuple[Path, ...]
    memfile_files: tuple[Path, ...]
    metadata_files: tuple[Path, ...]
    hashes: dict[str, str]
    parent_memory_digest: str
    parent_rootfs_digest: str

    @property
    def rootfs_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.rootfs_files)

    @property
    def memfile_bytes(self) -> int:
        return sum(_allocated(path) for path in self.memfile_files)

    @property
    def metadata_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.metadata_files)

    @property
    def key(self) -> str:
        return f"{self.snapshot_id}-v{self.version}"


@dataclass
class StageResult:
    rootfs_cache_hit: bool
    memfile_cache_hit: bool
    upload_seconds: float = 0.0
    download_seconds: float = 0.0
    memfile_seconds: float = 0.0
    metadata_seconds: float = 0.0
    upload_bytes: int = 0
    download_bytes: int = 0
    memfile_bytes: int = 0
    metadata_bytes: int = 0
    transfer_path: list[str] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        return (
            self.upload_seconds + self.download_seconds
            + self.memfile_seconds + self.metadata_seconds
        )


class SnapshotStorage:
    """Versioned rootfs cache plus one shared WARM memfile cache."""

    def __init__(self, spec: SnapshotSchedulingStorageSpec, *, run_id: str, arm_id: str,
                 task_uid: str, transfer_mode: str,
                 emit: Callable[[dict[str, Any]], None]) -> None:
        self.spec = spec
        self.transfer_mode = transfer_mode
        self.emit = emit
        self.local = {
            node: _safe_arm_directory(Path(root), run_id, arm_id, task_uid)
            for node, root in spec.local_roots.items()
        }
        self.shared = _shared_arm_directories(
            spec.shared_mounts, run_id, arm_id, task_uid,
        )
        self.warm = _safe_arm_directory(
            Path(spec.warm_root), run_id, arm_id, task_uid,
        )
        self.task_uid = task_uid
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._capacity_locks = {node: threading.Lock() for node in self.local}
        self._guard = threading.Lock()
        self._rootfs_cached: set[tuple[str, str]] = set()
        self._warm_cached: set[str] = set()
        self._shared_rootfs_cached: set[str] = set()
        self._shared_memfile_cached: set[str] = set()
        self._reserved_rootfs: set[tuple[str, str]] = set()
        self.shared_io_read_bytes = 0
        self.shared_io_write_bytes = 0
        self.memfile_transfer_bytes = 0
        self.memfile_metadata_transfer_bytes = 0
        self.memfile_transfer_seconds = 0.0
        self._io_lock = threading.Lock()
        self.peak_local_bytes = {node: 0 for node in self.local}
        self.peak_shared_bytes = 0
        self.peak_warm_bytes = 0

    def close(self) -> None:
        run_directories = {directory.parent for directory in self.local.values()}
        for node, directory in self.local.items():
            _remove_owned_directory(
                directory, root=Path(self.spec.local_roots[node]), task_uid=self.task_uid,
            )
        shared_node = next(iter(self.shared))
        run_directories.add(self.shared[shared_node].parent)
        _remove_owned_directory(
            self.shared[shared_node], root=Path(self.spec.shared_mounts[shared_node]),
            task_uid=self.task_uid,
        )
        run_directories.add(self.warm.parent)
        _remove_owned_directory(
            self.warm, root=Path(self.spec.warm_root), task_uid=self.task_uid,
        )
        for directory in run_directories:
            try:
                directory.rmdir()
            except OSError:
                # Another arm directory, if any, is never removed broadly.
                pass

    def sample(self) -> dict[str, Any]:
        local = {node: _allocated(path) for node, path in self.local.items()}
        shared = max((_allocated(path) for path in self.shared.values()), default=0)
        warm = _allocated(self.warm)
        for node, value in local.items():
            self.peak_local_bytes[node] = max(self.peak_local_bytes[node], value)
        self.peak_shared_bytes = max(self.peak_shared_bytes, shared)
        self.peak_warm_bytes = max(self.peak_warm_bytes, warm)
        return {"local_bytes": local, "shared_bytes": shared, "warm_bytes": warm}

    def _lock(self, package: ParentPackage, target: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault((package.key, target), threading.Lock())

    def rootfs_hit(self, package: ParentPackage, target: str) -> bool:
        return target == package.source_node or (package.key, target) in self._rootfs_cached

    def memfile_hit(self, package: ParentPackage) -> bool:
        return package.key in self._warm_cached or package.key in self._shared_memfile_cached

    def estimated_stage_bytes(self, package: ParentPackage, target: str) -> int:
        rootfs_copies = 2 if self.transfer_mode == "shared-relay" else 1
        rootfs = (
            0 if self.rootfs_hit(package, target)
            else rootfs_copies * package.rootfs_bytes
        )
        memfile = 0 if self.memfile_hit(package) else package.memfile_bytes
        return rootfs + memfile + package.metadata_bytes

    def feasible(self, package: ParentPackage, target: str, branch_bytes: int) -> bool:
        required = branch_bytes
        if not self.rootfs_hit(package, target):
            required += package.rootfs_bytes + package.metadata_bytes
        return shutil.disk_usage(self.local[target]).free >= required

    def reserve_branch(self, rollout_id: str, target: str, size_bytes: int) -> Path:
        path = self.local[target] / "branches" / f"{rollout_id}.cow"
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.posix_fallocate(descriptor, 0, size_bytes)
            block = hashlib.sha256(rollout_id.encode()).digest() * 128
            for offset in range(0, size_bytes, MIB):
                os.pwrite(descriptor, block, offset)
            os.pwrite(descriptor, b"branch-delta:" + rollout_id.encode(), max(0, size_bytes - 128))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.sample()
        return path

    def prepare_rollout(
        self, package: ParentPackage, target: str, rollout_id: str,
        branch_bytes: int,
    ) -> StageResult:
        """Atomically recheck disk, reserve the branch, and populate its cache."""
        with self._capacity_locks[target]:
            if not self.feasible(package, target, branch_bytes):
                raise OSError(
                    errno.ENOSPC,
                    f"{target} lacks space for branch and required rootfs cache",
                )
            branch = self.reserve_branch(rollout_id, target, branch_bytes)
            try:
                return self.stage(package, target)
            except Exception:
                branch.unlink(missing_ok=True)
                self.sample()
                raise

    def cache_memfile(self, package: ParentPackage) -> StageResult:
        with self._lock(package, "warm"):
            if self.memfile_hit(package):
                return StageResult(True, True)
            warm_free = shutil.disk_usage(self.warm).free
            destination_root = self.warm / package.key / "memfile"
            warm = warm_free >= package.memfile_bytes + package.metadata_bytes
            if not warm:
                destination_root = self.shared[package.source_node] / package.key / "memfile"
            result = StageResult(True, False)
            started = time.monotonic()
            for source in (*package.memfile_files, *package.metadata_files):
                relative = source.relative_to(package.directory)
                copied, _seconds = _copy_measured(source, destination_root / relative)
                if source in package.memfile_files:
                    result.memfile_bytes += copied
                else:
                    result.metadata_bytes += copied
            elapsed = time.monotonic() - started
            result.memfile_seconds = elapsed
            self.memfile_transfer_bytes += result.memfile_bytes
            self.memfile_metadata_transfer_bytes += result.metadata_bytes
            self.memfile_transfer_seconds += elapsed
            if warm:
                self._warm_cached.add(package.key)
                result.transfer_path.append(f"{package.source_node}-local->shared-dram")
            else:
                self._shared_memfile_cached.add(package.key)
                result.transfer_path.append(f"{package.source_node}-local->shared-ssd")
                with self._io_lock:
                    self.shared_io_write_bytes += result.memfile_bytes + result.metadata_bytes
            self.sample()
            return result

    def stage(self, package: ParentPackage, target: str) -> StageResult:
        if target == package.source_node:
            return StageResult(True, self.memfile_hit(package))
        with self._lock(package, target):
            if self.rootfs_hit(package, target):
                return StageResult(True, self.memfile_hit(package))
            result = StageResult(False, self.memfile_hit(package))
            target_root = self.local[target] / "cache" / package.key / "rootfs"
            if self.transfer_mode == "peer-direct":
                started = time.monotonic()
                for source in (*package.rootfs_files, *package.metadata_files):
                    relative = source.relative_to(package.directory)
                    copied, _seconds = _copy_measured(source, target_root / relative)
                    if source in package.rootfs_files:
                        result.download_bytes += copied
                    else:
                        result.metadata_bytes += copied
                result.download_seconds = time.monotonic() - started
                result.transfer_path.append(
                    f"{package.source_node}-local-xfs->{target}-local-xfs"
                )
            elif self.transfer_mode == "shared-relay":
                shared_root = self.shared[package.source_node] / package.key / "rootfs"
                if package.key not in self._shared_rootfs_cached:
                    started = time.monotonic()
                    for source in (*package.rootfs_files, *package.metadata_files):
                        relative = source.relative_to(package.directory)
                        copied, _seconds = _copy_measured(source, shared_root / relative)
                        if source in package.rootfs_files:
                            result.upload_bytes += copied
                        else:
                            result.metadata_bytes += copied
                    result.upload_seconds = time.monotonic() - started
                    self._shared_rootfs_cached.add(package.key)
                    with self._io_lock:
                        self.shared_io_write_bytes += result.upload_bytes + result.metadata_bytes
                    result.transfer_path.append(
                        f"{package.source_node}-local-xfs->{package.source_node}-nfs"
                    )
                started = time.monotonic()
                downloaded_metadata = 0
                for source in (*package.rootfs_files, *package.metadata_files):
                    relative = source.relative_to(package.directory)
                    nfs_source = self.shared[target] / package.key / "rootfs" / relative
                    copied, _seconds = _copy_measured(nfs_source, target_root / relative)
                    if source in package.rootfs_files:
                        result.download_bytes += copied
                    else:
                        downloaded_metadata += copied
                        result.metadata_bytes += copied
                result.download_seconds = time.monotonic() - started
                with self._io_lock:
                    self.shared_io_read_bytes += result.download_bytes + downloaded_metadata
                result.transfer_path.append(f"{target}-nfs->{target}-local-xfs")
            else:
                raise ValueError(f"unsupported snapshot transfer mode: {self.transfer_mode}")
            for source in (*package.rootfs_files, *package.metadata_files):
                relative = source.relative_to(package.directory)
                destination = target_root / relative
                if _digest(destination) != package.hashes[str(relative)]:
                    raise RuntimeError(f"staged snapshot dependency hash mismatch: {destination}")
            self._rootfs_cached.add((package.key, target))
            self.sample()
            return result


@dataclass
class NodeState:
    node_id: str
    slots: int
    memory_limit_mib: int
    vm_memory_mib: int
    active: int = 0
    waiting: int = 0
    completed: int = 0
    service_history: list[float] = field(default_factory=list)
    restore_history: list[float] = field(default_factory=list)
    peak_active: int = 0

    @property
    def available(self) -> bool:
        return self.active < self.slots and (
            (self.active + 1) * self.vm_memory_mib <= self.memory_limit_mib
        )

    def estimated_queue(self, default_service: float) -> float:
        service = statistics.fmean(self.service_history[-32:]) if self.service_history else default_service
        ahead = max(0, self.active + self.waiting + 1 - self.slots)
        return (ahead / self.slots) * service

    def estimated_restore(self, default_restore: float) -> float:
        return statistics.fmean(self.restore_history[-32:]) if self.restore_history else default_restore


class SnapshotScheduler:
    """Pure node decision and bounded per-node reservation."""

    def __init__(self, nodes: list[str], config: SnapshotBranchingSpec, *,
                 vm_memory_mib: int) -> None:
        self.config = config
        self.nodes = {
            node: NodeState(
                node, config.per_node_concurrency, config.per_node_memory_mib,
                vm_memory_mib,
            )
            for node in nodes
        }
        self.condition = threading.Condition()

    def choose(self, source: str, *, feasible: dict[str, bool],
               stage_seconds: dict[str, float], policy: str) -> tuple[str | None, dict[str, Any]]:
        with self.condition:
            candidates = {}
            for node, state in self.nodes.items():
                queue = state.estimated_queue(1.0)
                restore = state.estimated_restore(self.config.initial_restore_seconds)
                candidates[node] = {
                    "feasible": feasible[node],
                    "queue_seconds": queue,
                    "stage_seconds": stage_seconds[node],
                    "restore_seconds": restore,
                    "total_seconds": queue + stage_seconds[node] + restore,
                    "active": state.active,
                    "waiting": state.waiting,
                }
            if policy == "affinity":
                selected = source if feasible[source] else None
            elif policy == "load_aware":
                available = [node for node in self.nodes if feasible[node]]
                selected = min(
                    available,
                    key=lambda node: (candidates[node]["queue_seconds"], node),
                ) if available else None
            elif policy == "snapshot_aware":
                available = [node for node in self.nodes if feasible[node]]
                selected = min(
                    available,
                    key=lambda node: (candidates[node]["total_seconds"], node),
                ) if available else None
                if selected is not None and selected != source and feasible[source]:
                    benefit = (
                        candidates[source]["total_seconds"]
                        - candidates[selected]["total_seconds"]
                    )
                    if benefit <= self.config.migration_threshold_seconds:
                        selected = source
            else:
                raise ValueError(f"unsupported snapshot scheduling policy: {policy}")
            if selected is not None:
                self.nodes[selected].waiting += 1
            return selected, {"policy": policy, "selected": selected, "candidates": candidates}

    def acquire(self, node: str) -> float:
        started = time.monotonic()
        with self.condition:
            state = self.nodes[node]
            while not state.available:
                self.condition.wait(0.2)
            state.waiting -= 1
            state.active += 1
            state.peak_active = max(state.peak_active, state.active)
        return time.monotonic() - started

    def release(self, node: str, *, service_seconds: float, restore_seconds: float) -> None:
        with self.condition:
            state = self.nodes[node]
            state.active -= 1
            state.completed += 1
            state.service_history.append(service_seconds)
            state.restore_history.append(restore_seconds)
            self.condition.notify_all()


class RuntimeResourceMonitor:
    """Sample actual VM cgroup charges and study storage while an arm runs."""

    def __init__(self, domain: PriorityDomain, storage: SnapshotStorage,
                 interval: float, numa_to_node: dict[int, str]) -> None:
        self.domain = domain
        self.storage = storage
        self.interval = interval
        self.numa_to_node = numa_to_node
        self.peak_local_dram_bytes: dict[str, int] = {}
        self.samples = 0
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="snapshot-resource-monitor")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _sample(self) -> None:
        charges: dict[str, int] = {}
        for (index, role), path in list(self.domain.leaves.items()):
            if role != "tool":
                continue
            placement = self.domain.placements.get(index)
            if placement is None:
                continue
            node = self.numa_to_node.get(int(placement[1]))
            if node is None:
                continue
            try:
                charges[node] = charges.get(node, 0) + int(
                    (path / "memory.current").read_text(encoding="ascii").strip()
                )
            except (FileNotFoundError, OSError, ValueError):
                continue
        for node, value in charges.items():
            self.peak_local_dram_bytes[node] = max(
                self.peak_local_dram_bytes.get(node, 0), value,
            )
        self.storage.sample()
        self.samples += 1

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._sample()
            except Exception as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")
        try:
            self._sample()
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")


def _python_command(source: str) -> str:
    encoded = base64.b64encode(source.encode()).decode()
    return f"python3 -c \"import base64;exec(base64.b64decode('{encoded}'))\""


PARENT_DAEMON = r'''
import hashlib, json, os, socket
root = os.environ.get("CLAWBOX_PARENT_ROOT", "/workspace/clawbox-parent")
size = int(os.environ["CLAWBOX_PARENT_MEMORY_MIB"]) * 1024 * 1024
seed = os.environ["CLAWBOX_PARENT_ID"].encode()
state = bytearray(size)
pattern = hashlib.sha256(seed).digest()
for offset in range(0, size, 4096):
    state[offset:offset+len(pattern)] = pattern
def digest(): return hashlib.sha256(state).hexdigest()
sock_path = "/tmp/clawbox-parent-memory.sock"
try: os.unlink(sock_path)
except FileNotFoundError: pass
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(sock_path); server.listen(16)
with open(root + "/memory-ready.json", "w") as stream:
    json.dump({"digest": digest(), "bytes": len(state)}, stream); stream.flush(); os.fsync(stream.fileno())
while True:
    conn, _ = server.accept()
    with conn:
        command = conn.recv(4096).decode()
        if command.startswith("mutate:"):
            value = hashlib.sha256(command.encode()).digest()
            for offset in range(0, min(len(state), 1024 * 1024), 4096):
                state[offset:offset+len(value)] = value
        conn.sendall((digest() + "\n").encode())
'''


MEMORY_CLIENT = r'''
import socket, sys
s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect('/tmp/clawbox-parent-memory.sock')
s.sendall(sys.argv[1].encode())
print(s.recv(4096).decode().strip())
'''


BRANCH_WORKLOAD = r'''
import hashlib, json, os, socket, sys
rollout, expected, root, memory_mib, file_mib, rounds = sys.argv[1:]
def memory(command):
    s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect('/tmp/clawbox-parent-memory.sock'); s.sendall(command.encode())
    value=s.recv(4096).decode().strip(); s.close(); return value
initial = memory('query')
if initial != expected: raise RuntimeError('parent memory mismatch: '+initial+' != '+expected)
payload = bytearray(int(memory_mib)*1024*1024)
for offset in range(0, len(payload), 4096): payload[offset] = (offset//4096) & 255
cpu = hashlib.sha256(bytes(payload[:1024*1024])).digest()
for _ in range(int(rounds)): cpu = hashlib.sha256(cpu + payload[:1024*1024]).digest()
path = root + '/branch-state.bin'
chunk = hashlib.sha256(rollout.encode()).digest() * (1024*1024//32)
with open(path, 'wb', buffering=0) as stream:
    for _ in range(int(file_mib)): stream.write(chunk)
    os.fsync(stream.fileno())
with open(path, 'r+b', buffering=0) as stream:
    stream.seek(max(0, int(file_mib)*1024*1024-4096)); stream.write(cpu*128); os.fsync(stream.fileno())
mutated = memory('mutate:'+rollout)
with open(root + '/branch-id.txt', 'w') as stream:
    stream.write(rollout); stream.flush(); os.fsync(stream.fileno())
print(json.dumps({'initial_memory_digest':initial,'mutated_memory_digest':mutated,
 'cpu_digest':cpu.hex(),'memory_checksum':sum(payload[::4096]),'file_bytes':os.path.getsize(path)}))
'''


class SnapshotSchedulingRunner:
    def __init__(self, *, arm: ExperimentArm, client: CubeSandboxClient,
                 run_id: str, attempt_id: str, task_uid: str,
                 experiment_id: str, output_root: Path) -> None:
        self.arm = arm
        self.client = client
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.task_uid = task_uid
        self.experiment_id = experiment_id
        self.output_root = output_root
        self.config = arm.execution.snapshot_branching
        self.storage_spec = arm.resources.snapshot_scheduling_storage
        if self.config is None or self.storage_spec is None:
            raise ValueError("snapshot scheduling runner requires its scenario configuration")
        self.events_path = output_root / "events" / f"{arm.arm_id}.jsonl"
        self.rollouts_path = output_root / "snapshot-scheduling" / f"{arm.arm_id}-rollouts.jsonl"
        self.parents_path = output_root / "snapshot-scheduling" / f"{arm.arm_id}-parents.json"
        self._event_lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        record = {
            "run_id": self.run_id, "attempt_id": self.attempt_id,
            "arm_id": self.arm.arm_id, "unix_s": time.time(), **event,
        }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self._event_lock:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as stream:
                stream.write(encoded)

    def _ownership(self, session_id: str) -> Ownership:
        return Ownership(
            self.run_id, self.attempt_id, self.task_uid, self.experiment_id,
            session_id, self.arm.policy.name,
        )

    def _create_vm(self, template: str, node: str, index: int,
                   domain: PriorityDomain, session_id: str) -> tuple[Any, dict[str, Any]]:
        compute = {item.node_id: item for item in self.arm.resources.compute_nodes}[node]
        domain.configure_topology_placement(index, compute.cpus, compute.numa_node)
        domain.prepare(index, "tool")
        sandbox = self.client.create_sandbox(
            template=template,
            node_name=self.arm.resources.target_node,
            ownership=self._ownership(session_id),
            env_vars={"CLAWBOX_CGROUP_TOKEN": domain.token(index, "tool")},
            allow_internet_access=False,
        )
        evidence = domain.verify(index, "tool", self.client.sandbox_id(sandbox))
        return sandbox, evidence

    def _prepare_parent(self, parent_index: int, source_node: str,
                        storage: SnapshotStorage, domain: PriorityDomain,
                        direct_root: Path) -> ParentPackage:
        parent_id = f"parent-{parent_index:02d}"
        parent, placement = self._create_vm(
            self.arm.sandbox.template, source_node, parent_index, domain, parent_id,
        )
        root = self.arm.sandbox.workspace.rstrip("/") + "/clawbox-parent"
        payload_bytes = self.config.parent_rootfs_payload_mib * MIB
        token = hashlib.sha256(parent_id.encode()).hexdigest()
        setup = f'''
import hashlib, json, os
root={root!r}; os.makedirs(root, exist_ok=True)
token={token!r}.encode(); size={payload_bytes}
with open(root+'/rootfs-payload.txt','wb',buffering=0) as stream:
    remaining=size
    while remaining:
        chunk=(token*min(65536, remaining//len(token)+1))[:min(4*1024*1024,remaining)]
        stream.write(chunk); remaining-=len(chunk)
    os.fsync(stream.fileno())
digest=hashlib.sha256(open(root+'/rootfs-payload.txt','rb').read()).hexdigest()
with open(root+'/parent.json','w') as stream:
    json.dump({{'parent_id':{parent_id!r},'source_node':{source_node!r},'rootfs_digest':digest}},stream)
    stream.flush(); os.fsync(stream.fileno())
'''
        self.client.run_command(parent, _python_command(setup), timeout_s=self.config.command_timeout_seconds)
        self.client.write_file(parent, "/tmp/clawbox-parent-daemon.py", PARENT_DAEMON)
        self.client.write_file(parent, "/tmp/clawbox-memory-client.py", MEMORY_CLIENT)
        daemon_command = (
            f"CLAWBOX_PARENT_ROOT={root} "
            f"CLAWBOX_PARENT_MEMORY_MIB={self.config.parent_memory_state_mib} "
            f"CLAWBOX_PARENT_ID={parent_id} "
            "nohup python3 /tmp/clawbox-parent-daemon.py "
            ">/tmp/clawbox-parent-daemon.log 2>&1 </dev/null &"
        )
        self.client.run_command(parent, daemon_command, timeout_s=30)
        if not self.client.wait_file_nonempty(
            parent, root + "/memory-ready.json", timeout_s=30,
        ):
            raise RuntimeError(f"{parent_id}: parent memory daemon did not become ready")
        ready = json.loads(self.client.read_file(parent, root + "/memory-ready.json"))
        rootfs_payload = self.client.read_file(parent, root + "/rootfs-payload.txt")
        rootfs_bytes = rootfs_payload.encode()
        rootfs_digest = hashlib.sha256(rootfs_bytes).hexdigest()
        if rootfs_digest != json.loads(
            self.client.read_file(parent, root + "/parent.json")
        )["rootfs_digest"]:
            raise RuntimeError(f"{parent_id}: rootfs export changed before snapshot")
        snapshot_started = time.monotonic()
        snapshot_id = self.client.create_snapshot(parent, self._ownership(parent_id))
        native_snapshot_seconds = time.monotonic() - snapshot_started
        source = storage.local[source_node] / "parents" / parent_id
        source.mkdir(parents=True, exist_ok=False)
        rootfs_path = source / "rootfs" / "rootfs-payload.txt"
        rootfs_path.parent.mkdir(parents=True)
        rootfs_path.write_bytes(rootfs_bytes)
        direct_path = direct_root / parent_id / "parent.mem"
        direct_path.parent.mkdir(parents=True, exist_ok=False)
        manifest = self.client.pause_sandbox(
            parent, tier="cold", memory_snapshot_path=str(direct_path),
            generation=1, snapshot_mechanism="full-copy",
        ) or {}
        mem_root = source / "memfile"
        mem_root.mkdir()
        direct_files = [direct_path]
        for suffix in (".lineage.json", ".compression.json"):
            candidate = Path(str(direct_path) + suffix)
            if candidate.is_file():
                direct_files.append(candidate)
        layers = Path(str(direct_path) + ".layers")
        if layers.is_dir():
            direct_files.extend(item for item in layers.rglob("*") if item.is_file())
        memfiles = []
        metadata = []
        for item in direct_files:
            relative = item.relative_to(direct_path.parent)
            target = mem_root / relative
            _copy_measured(item, target)
            (metadata if item.suffix == ".json" else memfiles).append(target)
        package_manifest = source / "snapshot-package.json"
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "version": self.config.snapshot_version,
            "parent_id": parent_id,
            "source_node": source_node,
            "template_id": self.arm.sandbox.template,
            "native_snapshot_seconds": native_snapshot_seconds,
            "native_snapshot_manifest": manifest,
            "rootfs_scope": "workload_modified_rootfs_payload_plus_immutable_template_dependency",
            "memfile_scope": "direct_full_copy_cube_memory_snapshot",
        }
        package_manifest.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        metadata.append(package_manifest)
        files = (rootfs_path, *memfiles, *metadata)
        hashes = {
            str(path.relative_to(source)): _digest(path) for path in files
        }
        package = ParentPackage(
            parent_id, source_node, snapshot_id, self.config.snapshot_version,
            source, (rootfs_path,), tuple(memfiles), tuple(metadata), hashes,
            str(ready["digest"]), rootfs_digest,
        )
        self.client.kill_sandbox(parent)
        self.emit({
            "event": "snapshot_parent_ready", "parent_id": parent_id,
            "source_node": source_node, "snapshot_id": snapshot_id,
            "native_snapshot_seconds": native_snapshot_seconds,
            "rootfs_bytes": package.rootfs_bytes,
            "memfile_bytes": package.memfile_bytes,
            "metadata_bytes": package.metadata_bytes,
            "placement": placement,
        })
        return package

    def _run_rollout(self, rollout_index: int, package: ParentPackage,
                     branch_index: int, scheduler: SnapshotScheduler,
                     storage: SnapshotStorage, domain: PriorityDomain,
                     offered_monotonic: float) -> dict[str, Any]:
        rollout_id = f"{package.parent_id}-branch-{branch_index:02d}"
        branch_bytes = self.config.branch_disk_reservation_mib * MIB
        nodes = list(scheduler.nodes)
        feasible = {
            node: storage.feasible(package, node, branch_bytes) for node in nodes
        }
        bandwidth = self.config.initial_transfer_mib_per_second * MIB
        stage_estimates = {
            node: (storage.estimated_stage_bytes(package, node) / bandwidth)
            for node in nodes
        }
        selected, prediction = scheduler.choose(
            package.source_node, feasible=feasible, stage_seconds=stage_estimates,
            policy=self.arm.execution.placement_policy,
        )
        decisions = [prediction]

        def admission_failure(execution_node: str | None, failure: str) -> dict[str, Any]:
            recorded_prediction: dict[str, Any] = (
                decisions[0] if len(decisions) == 1 else {
                    "policy": self.arm.execution.placement_policy,
                    "selected": execution_node,
                    "capacity_reselection": True,
                    "attempts": decisions,
                }
            )
            return {
                "parent_id": package.parent_id, "branch_id": branch_index,
                "rollout_id": rollout_id, "source_node": package.source_node,
                "execution_node": execution_node, "snapshot_id": package.snapshot_id,
                "snapshot_version": package.version, "storage_location": "source-local",
                "prediction": recorded_prediction, "status": "admission_failed",
                "failure": failure,
                "arrival_unix_s": time.time(), "e2e_seconds": time.monotonic() - offered_monotonic,
                "queue_seconds": 0.0, "stage_seconds": 0.0, "restore_seconds": 0.0,
                "tool_seconds": 0.0, "rootfs_cache_hit": False,
                "memfile_cache_hit": storage.memfile_hit(package),
                "transfer_path": [], "rootfs_transfer_bytes": 0,
                "memfile_transfer_bytes": 0, "metadata_transfer_bytes": 0,
                "shared_io_read_bytes": 0, "shared_io_write_bytes": 0,
            }

        if selected is None:
            return admission_failure(None, "no node satisfies local disk capacity")
        stage: StageResult | None = None
        capacity_errors: list[str] = []
        excluded: set[str] = set()
        while selected is not None and len(excluded) < len(nodes):
            try:
                stage = storage.prepare_rollout(
                    package, selected, rollout_id, branch_bytes,
                )
                break
            except OSError as exc:
                capacity_errors.append(f"{selected}: {exc}")
                excluded.add(selected)
                with scheduler.condition:
                    scheduler.nodes[selected].waiting -= 1
                    scheduler.condition.notify_all()
                if self.arm.execution.placement_policy == "affinity":
                    selected = None
                    break
                feasible = {
                    node: node not in excluded
                    and storage.feasible(package, node, branch_bytes)
                    for node in nodes
                }
                stage_estimates = {
                    node: storage.estimated_stage_bytes(package, node) / bandwidth
                    for node in nodes
                }
                selected, retry_prediction = scheduler.choose(
                    package.source_node, feasible=feasible,
                    stage_seconds=stage_estimates,
                    policy=self.arm.execution.placement_policy,
                )
                decisions.append(retry_prediction)
        if stage is None:
            selected_for_record = next(iter(excluded), None)
            return admission_failure(
                selected_for_record,
                "local disk capacity exhausted after reselection: "
                + "; ".join(capacity_errors),
            )
        if len(decisions) > 1:
            prediction = {
                "policy": self.arm.execution.placement_policy,
                "selected": selected,
                "capacity_reselection": True,
                "attempts": decisions,
            }
        wait_after_stage = scheduler.acquire(selected)
        acquired = time.monotonic()
        sandbox = None
        restore_seconds = tool_seconds = 0.0
        started_service = acquired
        try:
            restore_started = time.monotonic()
            sandbox, placement = self._create_vm(
                package.snapshot_id, selected,
                len(self.config.parent_nodes) + rollout_index,
                domain, rollout_id,
            )
            foreign_local = next(
                path for node, path in self.storage_spec.local_roots.items()
                if node != selected
            )
            isolation = self.client.run_command(
                sandbox,
                _python_command(
                    f"import os,sys;sys.exit(1 if os.path.exists({foreign_local!r}) else 0)"
                ),
                timeout_s=30,
            )
            if isolation.exit_code != 0:
                raise RuntimeError(
                    f"rollout can access foreign logical-node local path: {foreign_local}"
                )
            initial = self.client.run_command(
                sandbox, "python3 /tmp/clawbox-memory-client.py query",
                timeout_s=30,
            ).stdout.strip()
            if initial != package.parent_memory_digest:
                raise RuntimeError(
                    f"restored VM memory mismatch: {initial} != {package.parent_memory_digest}"
                )
            rootfs = self.client.run_command(
                sandbox,
                f"sha256sum {self.arm.sandbox.workspace.rstrip('/')}/clawbox-parent/rootfs-payload.txt",
                timeout_s=30,
            ).stdout.split()[0]
            if rootfs != package.parent_rootfs_digest:
                raise RuntimeError(
                    f"restored rootfs mismatch: {rootfs} != {package.parent_rootfs_digest}"
                )
            restore_seconds = time.monotonic() - restore_started
            self.client.write_file(sandbox, "/tmp/clawbox-branch-workload.py", BRANCH_WORKLOAD)
            command = " ".join([
                "python3", "/tmp/clawbox-branch-workload.py", rollout_id,
                package.parent_memory_digest,
                self.arm.sandbox.workspace.rstrip("/") + "/clawbox-parent",
                str(self.config.branch_memory_work_mib),
                str(self.config.branch_file_work_mib),
                str(self.config.branch_cpu_rounds),
            ])
            tool_started = time.monotonic()
            result = self.client.run_command(
                sandbox, command, timeout_s=self.config.command_timeout_seconds,
            )
            tool_seconds = time.monotonic() - tool_started
            if result.exit_code != 0:
                raise RuntimeError(f"branch workload failed: {result.stderr.strip()}")
            workload = json.loads(result.stdout.strip().splitlines()[-1])
            if workload["initial_memory_digest"] != package.parent_memory_digest:
                raise RuntimeError("branch observed wrong initial parent memory")
            if workload["mutated_memory_digest"] == package.parent_memory_digest:
                raise RuntimeError("branch memory mutation did not change private state")
            status, failure = "succeeded", None
        except Exception as exc:
            placement = None
            workload = None
            status, failure = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            if sandbox is not None:
                self.client.kill_sandbox(sandbox)
            service = time.monotonic() - started_service
            scheduler.release(
                selected, service_seconds=service,
                restore_seconds=restore_seconds or self.config.initial_restore_seconds,
            )
        completed = time.monotonic()
        record = {
            "parent_id": package.parent_id, "branch_id": branch_index,
            "rollout_id": rollout_id, "source_node": package.source_node,
            "execution_node": selected, "snapshot_id": package.snapshot_id,
            "snapshot_version": package.version,
            "storage_location": "target-local-cache" if selected != package.source_node else "source-local",
            "cross_node_restore_mode": (
                (
                    "simulated_peer_direct_copy"
                    if self.config.transfer_mode == "peer-direct"
                    else "simulated_cross_node_restore"
                )
                if selected != package.source_node else "same_node_native_restore"
            ),
            "prediction": prediction,
            "transfer_path": stage.transfer_path,
            "rootfs_cache_hit": stage.rootfs_cache_hit,
            "memfile_cache_hit": stage.memfile_cache_hit,
            "queue_seconds": max(0.0, acquired - offered_monotonic - stage.total_seconds),
            "stage_seconds": stage.total_seconds,
            "rootfs_upload_seconds": stage.upload_seconds,
            "rootfs_download_seconds": stage.download_seconds,
            "memfile_prepare_seconds": stage.memfile_seconds,
            "metadata_prepare_seconds": stage.metadata_seconds,
            "restore_seconds": restore_seconds, "tool_seconds": tool_seconds,
            "e2e_seconds": completed - offered_monotonic,
            "rootfs_transfer_bytes": stage.upload_bytes + stage.download_bytes,
            "memfile_transfer_bytes": stage.memfile_bytes,
            "metadata_transfer_bytes": stage.metadata_bytes,
            "shared_io_write_bytes": stage.upload_bytes + stage.metadata_bytes,
            "shared_io_read_bytes": stage.download_bytes,
            "status": status, "failure": failure,
            "foreign_local_path_inaccessible": status == "succeeded",
            "placement": placement, "workload": workload,
            "wait_after_stage_seconds": wait_after_stage,
        }
        self.emit({"event": "snapshot_rollout_terminal", **record})
        return record

    def run(self, *, result_path: Path, marker_path: Path) -> ResultEnvelope:
        started_at = utcnow()
        arm_started = time.monotonic()
        host_storage = validate_applied_storage(self.storage_spec)
        storage = SnapshotStorage(
            self.storage_spec, run_id=self.run_id, arm_id=self.arm.arm_id,
            task_uid=self.task_uid, transfer_mode=self.config.transfer_mode,
            emit=self.emit,
        )
        direct_root = _safe_arm_directory(
            Path(self.arm.resources.cold_snapshot_root), self.run_id,
            self.arm.arm_id, self.task_uid,
        )
        total_rollouts = len(self.config.parent_nodes) * self.config.branches_per_parent
        domain = PriorityDomain(
            Path(self.arm.resources.local_memory_cgroup),
            concurrency=len(self.config.parent_nodes) + total_rollouts,
            repetition=self.arm.repetition,
            legacy=False,
            journal_directory=self.output_root / "priority-domains",
        )
        domain.start()
        monitor = RuntimeResourceMonitor(
            domain, storage, self.arm.execution.memory_sample_interval_seconds,
            {node.numa_node: node.node_id for node in self.arm.resources.compute_nodes},
        )
        monitor.start()
        parents: list[ParentPackage] = []
        rollouts: list[dict[str, Any]] = []
        unexpected: BaseException | None = None
        preparation_started = time.monotonic()
        try:
            for index, source in enumerate(self.config.parent_nodes):
                package = self._prepare_parent(index, source, storage, domain, direct_root)
                parents.append(package)
                warm = storage.cache_memfile(package)
                self.emit({
                    "event": "snapshot_memfile_cached", "parent_id": package.parent_id,
                    "cache_hit": warm.memfile_cache_hit,
                    "bytes": warm.memfile_bytes + warm.metadata_bytes,
                    "seconds": warm.memfile_seconds + warm.metadata_seconds,
                    "path": warm.transfer_path,
                })
            preparation_seconds = time.monotonic() - preparation_started
            self.parents_path.parent.mkdir(parents=True, exist_ok=True)
            self.parents_path.write_text(json.dumps({
                "cross_node_restore_mode": (
                    "simulated_peer_direct_copy"
                    if self.config.transfer_mode == "peer-direct"
                    else "simulated_cross_node_restore"
                ),
                "parents": [{
                    "parent_id": item.parent_id, "source_node": item.source_node,
                    "snapshot_id": item.snapshot_id, "version": item.version,
                    "rootfs_bytes": item.rootfs_bytes,
                    "memfile_bytes": item.memfile_bytes,
                    "metadata_bytes": item.metadata_bytes,
                    "hashes": item.hashes,
                } for item in parents],
            }, indent=2, sort_keys=True), encoding="utf-8")
            scheduler = SnapshotScheduler(
                list(storage.local), self.config, vm_memory_mib=self.arm.sandbox.memory_mib,
            )
            work = [
                (package, branch)
                for package in parents
                for branch in range(self.config.branches_per_parent)
            ]
            random.Random(self.arm.execution.random_seed).shuffle(work)
            rollout_started = time.monotonic()
            futures = []
            cursor = 0
            with ThreadPoolExecutor(
                max_workers=self.arm.concurrency,
                thread_name_prefix="snapshot-rollout",
            ) as pool:
                for batch_index, count in enumerate(self.config.arrival_batches):
                    for package, branch in work[cursor:cursor + count]:
                        offered = time.monotonic()
                        rollout_index = parents.index(package) * self.config.branches_per_parent + branch
                        futures.append(pool.submit(
                            self._run_rollout, rollout_index, package, branch,
                            scheduler, storage, domain, offered,
                        ))
                    cursor += count
                    if batch_index + 1 < len(self.config.arrival_batches):
                        time.sleep(self.config.arrival_batch_interval_seconds)
                for future in as_completed(futures):
                    rollouts.append(future.result())
            rollout_makespan = time.monotonic() - rollout_started
        except BaseException as exc:
            unexpected = exc
            preparation_seconds = time.monotonic() - preparation_started
            rollout_makespan = 0.0
        finally:
            monitor.stop()
            for package in parents:
                try:
                    self.client.delete_snapshot(package.snapshot_id)
                except Exception as exc:
                    if "not found" not in str(exc).lower() and unexpected is None:
                        unexpected = exc
            try:
                domain.close()
            except Exception as exc:
                if unexpected is None:
                    unexpected = exc
            if direct_root.exists():
                try:
                    _remove_owned_directory(
                        direct_root,
                        root=Path(self.arm.resources.cold_snapshot_root),
                        task_uid=self.task_uid,
                    )
                    try:
                        direct_root.parent.rmdir()
                    except OSError:
                        pass
                except Exception as exc:
                    if unexpected is None:
                        unexpected = RuntimeError(
                            f"direct snapshot cleanup failed: {exc}"
                        )
            storage.sample()

        rollouts.sort(key=lambda item: item["rollout_id"])
        self.rollouts_path.parent.mkdir(parents=True, exist_ok=True)
        with self.rollouts_path.open("w", encoding="utf-8") as stream:
            for item in rollouts:
                stream.write(json.dumps(item, sort_keys=True) + "\n")
        succeeded = [item for item in rollouts if item["status"] == "succeeded"]
        admission = [item for item in rollouts if item["status"] == "admission_failed"]
        failed = [item for item in rollouts if item["status"] == "failed"]
        cross = [item for item in succeeded if item["execution_node"] != item["source_node"]]
        e2e = [float(item["e2e_seconds"]) for item in rollouts]
        queue = [float(item["queue_seconds"]) for item in rollouts]
        transfer = [float(item["stage_seconds"]) for item in rollouts]
        restore = [float(item["restore_seconds"]) for item in succeeded]
        rootfs_bytes = sum(int(item["rootfs_transfer_bytes"]) for item in rollouts)
        memfile_bytes = (
            storage.memfile_transfer_bytes
            + sum(int(item["memfile_transfer_bytes"]) for item in rollouts)
        )
        metadata_bytes = (
            storage.memfile_metadata_transfer_bytes
            + sum(int(item["metadata_transfer_bytes"]) for item in rollouts)
        )
        rootfs_hits = sum(bool(item["rootfs_cache_hit"]) for item in rollouts)
        memfile_hits = sum(bool(item["memfile_cache_hit"]) for item in rollouts)
        packages = {package.parent_id: package for package in parents}
        rootfs_copy_count = 2 if self.config.transfer_mode == "shared-relay" else 1
        theoretical_rootfs = sum(
            rootfs_copy_count * packages[item["parent_id"]].rootfs_bytes
            for item in succeeded
            if item["execution_node"] != item["source_node"]
        )
        theoretical_memfile = sum(
            packages[item["parent_id"]].memfile_bytes for item in succeeded
        )
        cleanup_verified = True
        try:
            storage.close()
        except Exception as exc:
            cleanup_verified = False
            if unexpected is None:
                unexpected = RuntimeError(
                    f"snapshot scheduling storage cleanup failed: {exc}"
                )
        validation = unexpected is None and not failed and len(rollouts) == total_rollouts
        status = RunStatus.SUCCEEDED if validation else RunStatus.FAILED
        failure = None if unexpected is None else f"{type(unexpected).__name__}: {unexpected}"
        if failed and failure is None:
            failure = f"{len(failed)} rollout workloads failed"
        performance = {
            "duration_seconds": time.monotonic() - arm_started,
            "preparation_seconds": preparation_seconds,
            "makespan_seconds": rollout_makespan,
            "rollouts_per_second": len(succeeded) / max(rollout_makespan, 1e-9),
            "offered_rollouts_per_second": len(rollouts) / max(rollout_makespan, 1e-9),
            "tool_steps_per_second": (len(succeeded) * 3) / max(rollout_makespan, 1e-9),
            "rollout_e2e_seconds": _distribution(e2e),
            "queue_seconds": _distribution(queue),
            "snapshot_transfer_seconds": _distribution(transfer),
            "memfile_cache_prepare_seconds": storage.memfile_transfer_seconds,
            "restore_seconds": _distribution(restore),
            "cross_node_restores": len(cross),
            "cross_node_restore_mode": (
                "simulated_peer_direct_copy"
                if self.config.transfer_mode == "peer-direct"
                else "simulated_cross_node_restore"
            ),
            "snapshot_transfer_mode": self.config.transfer_mode,
            "rootfs_transfer_bytes": rootfs_bytes,
            "memfile_transfer_bytes": memfile_bytes,
            "metadata_transfer_bytes": metadata_bytes,
            "transfer_bytes": rootfs_bytes + memfile_bytes + metadata_bytes,
            "shared_storage_read_bytes": storage.shared_io_read_bytes,
            "shared_storage_write_bytes": storage.shared_io_write_bytes,
            "rootfs_cache_hit_rate": rootfs_hits / max(len(rollouts), 1),
            "memfile_cache_hit_rate": memfile_hits / max(len(rollouts), 1),
            "transfer_savings_bytes": max(
                0, theoretical_rootfs + theoretical_memfile - rootfs_bytes - memfile_bytes,
            ),
            "admission_failures": len(admission),
            "completed_rollouts": len(succeeded),
            "failed_rollouts": len(failed),
            "offered_rollouts": len(rollouts),
            "queued_rollouts_at_end": 0,
            "node_peak_active": {
                node: state.peak_active for node, state in (
                    scheduler.nodes.items() if 'scheduler' in locals() else []
                )
            },
        }
        result = ResultEnvelope(
            run_id=self.run_id, attempt_id=self.attempt_id,
            sandbox_task_uid=self.task_uid, experiment_id=self.experiment_id,
            arm=self.arm,
            provenance={
                "scenario": "snapshot-aware-scheduling-across-local-and-shared-storage",
                "cube_native_snapshot_clone": True,
                "cube_restore_from_copied_paths": False,
                "cross_node_restore_label": (
                    "simulated_peer_direct_copy"
                    if self.config.transfer_mode == "peer-direct"
                    else "simulated_cross_node_restore"
                ),
                "rootfs_package_scope": (
                    "workload-modified rootfs payload plus immutable Cube template dependency; "
                    "not a portable full-rootfs image"
                ),
                "memfile_package_scope": "Cube direct full-copy memory snapshot and metadata",
                "host_receipt": self.storage_spec.host_receipt,
                "host_storage": host_storage,
                "shared_storage_io_measurement": (
                    "bytes issued by measured copy loops through distinct NFS mounts; "
                    "not inferred transfer time"
                ),
                "peer_direct_io_measurement": (
                    "one measured same-host cross-XFS copy; no NFS relay and no "
                    "node-to-node network emulation"
                    if self.config.transfer_mode == "peer-direct" else None
                ),
            },
            status=status,
            failure_category=(None if status is RunStatus.SUCCEEDED else FailureCategory.INFRASTRUCTURE),
            started_at=started_at, completed_at=utcnow(),
            correctness={
                "validation_passed": validation,
                "failure": failure,
                "rootfs_state_verified": validation and bool(succeeded),
                "vm_memory_state_verified": validation and bool(succeeded),
                "branch_isolation_verified": validation and len({
                    item.get("workload", {}).get("mutated_memory_digest")
                    for item in succeeded if item.get("workload")
                }) > 1,
                "parent_snapshot_immutable": validation and all(
                    item.get("workload", {}).get("initial_memory_digest")
                    == next((p.parent_memory_digest for p in parents if p.parent_id == item["parent_id"]), None)
                    for item in succeeded
                ),
                "real_two_hop_copy_verified": bool(cross) and all(
                    len(item["transfer_path"]) == 2
                    for item in cross if not item["rootfs_cache_hit"]
                ),
                "storage_isolation_verified": validation and all(
                    item.get("foreign_local_path_inaccessible") for item in succeeded
                ),
                "cache_reuse_verified": rootfs_hits > 0 and memfile_hits > 0,
                "capacity_constraint_enforced": all(
                    item["status"] == "admission_failed" and bool(item.get("failure"))
                    for item in admission
                ),
                "completed_rollouts": len(succeeded),
                "admission_failures": len(admission),
                "failed_rollouts": len(failed),
                "cleanup_verified": cleanup_verified,
            },
            performance=performance,
            memory={
                "peak_local_dram_bytes": monitor.peak_local_dram_bytes,
                "peak_local_storage_bytes": storage.peak_local_bytes,
                "peak_shared_ssd_bytes": storage.peak_shared_bytes,
                "peak_shared_dram_bytes": storage.peak_warm_bytes,
                "configured_shared_dram_bytes": self.storage_spec.warm_capacity_mib * MIB,
                "resource_sample_count": monitor.samples,
                "resource_sample_errors": monitor.errors,
            },
            artifacts={
                "events": str(self.events_path),
                "rollouts": str(self.rollouts_path),
                "parents": str(self.parents_path),
            },
        )
        result_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = result_path.with_suffix(".tmp")
        temporary.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, result_path)
        marker_path.write_text(self.arm.spec_digest, encoding="ascii")
        return result
