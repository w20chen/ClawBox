"""Live VM borrowing from the shared NUMA pool.

CubeSandbox places each microVM in a descendant cgroup.  Rebinding that leaf's
``cpuset.mems`` keeps the VM running and directs future allocations to the
shared node. Existing-page migration is kernel dependent and measured separately.
Capacity is reserved before the
rebind, so live borrowing and WARM snapshots cannot independently overcommit
the same physical pool.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from collections.abc import Callable

from .snapshot_pool import WarmSnapshotPool


@dataclass(frozen=True, slots=True)
class NumaBorrowRecord:
    sandbox_id: str
    cgroup: str
    reserved_bytes: int
    source_node: int
    target_node: int
    started_unix_s: float
    completed_unix_s: float
    service_seconds: float
    local_bytes_before: int
    local_bytes_after: int
    shared_bytes_before: int
    shared_bytes_after: int


def read_numa_lru_bytes(cgroup: Path) -> tuple[dict[int, int], int]:
    """Return per-node resident LRU bytes plus conservatively unattributed bytes."""
    rows: dict[str, dict[int, int]] = {}
    for line in (cgroup / "memory.numa_stat").read_text(encoding="ascii").splitlines():
        fields = line.split()
        values: dict[int, int] = {}
        for field in fields[1:]:
            node, separator, raw = field.partition("=")
            if separator and node.startswith("N"):
                values[int(node[1:])] = int(raw)
        rows[fields[0]] = values
    primary = ("inactive_anon", "active_anon", "inactive_file", "active_file", "unevictable")
    nodes = {node for name in primary for node in rows.get(name, {})}
    resident = {
        node: sum(rows.get(name, {}).get(node, 0) for name in primary)
        for node in nodes
    }
    charged = int((cgroup / "memory.current").read_text(encoding="ascii").strip())
    return resident, max(0, charged - sum(resident.values()))


class SandboxNumaBorrower:
    def __init__(self, cgroup_root: Path, *, local_node: int, shared_node: int,
                 shared_pool: WarmSnapshotPool,
                 cpus: str | None = None, node_id: str | None = None,
                 proc_root: Path = Path("/proc"),
                 cgroup_mount: Path = Path("/sys/fs/cgroup"),
                 writer: Callable[[Path, str], None] | None = None) -> None:
        root = cgroup_root.resolve()
        if local_node == shared_node:
            raise ValueError("LOCAL and shared NUMA nodes must differ")
        self.cgroup_root = root
        self.local_node = local_node
        self.shared_node = shared_node
        self.shared_pool = shared_pool
        self.cpus = cpus
        self.node_id = node_id
        self.placements: list[dict] = []
        self.proc_root = proc_root
        self.cgroup_mount = cgroup_mount.resolve()
        self.writer = writer or self._privileged_write
        self._lock = RLock()
        self._borrowed: dict[str, tuple[Path, int]] = {}

    @staticmethod
    def _privileged_write(path: Path, value: str) -> None:
        completed = subprocess.run(
            ["sudo", "-n", "tee", str(path)], input=value + "\n",
            text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(
                f"cannot update {path}: {completed.stderr.strip() or completed.returncode}"
            )

    def _leaf_for(self, sandbox_id: str) -> Path:
        leaves: set[Path] = set()
        for entry in self.proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if sandbox_id.encode() not in (entry / "cmdline").read_bytes():
                    continue
                unified = next(
                    line.partition("0::")[2]
                    for line in (entry / "cgroup").read_text(encoding="ascii").splitlines()
                    if line.startswith("0::")
                )
                leaf = (self.cgroup_mount / unified.lstrip("/")).resolve()
                leaf.relative_to(self.cgroup_root)
                if leaf == self.cgroup_root or not (leaf / "cpuset.mems").exists():
                    raise RuntimeError("VM process is not in a writable leaf cpuset")
                leaves.add(leaf)
            except (FileNotFoundError, PermissionError, ProcessLookupError, StopIteration):
                continue
        if len(leaves) != 1:
            raise RuntimeError(
                f"expected one cgroup for sandbox {sandbox_id}, found {sorted(map(str, leaves))}"
            )
        return next(iter(leaves))

    def _usage(self) -> tuple[int, int]:
        resident, unattributed = read_numa_lru_bytes(self.cgroup_root)
        return (
            resident.get(self.local_node, 0) + unattributed,
            resident.get(self.shared_node, 0),
        )

    def _move(self, sandbox_id: str, capacity_bytes: int, *, target_node: int,
              reserve: bool) -> NumaBorrowRecord:
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        leaf = self._leaf_for(sandbox_id)
        before_local, before_shared = self._usage()
        if reserve:
            self.shared_pool.reserve_borrow(sandbox_id, capacity_bytes)
        started_wall = time.time()
        started = time.monotonic()
        try:
            self._verify_cpus(leaf)
            self.writer(leaf / "cpuset.mems", str(target_node))
            effective = (leaf / "cpuset.mems.effective").read_text(encoding="ascii").strip()
            if effective != str(target_node):
                raise RuntimeError(
                    f"VM cpuset rebind did not take effect: requested {target_node}, got {effective}"
                )
        except Exception:
            if reserve:
                self.shared_pool.release_borrow(sandbox_id)
            raise
        if not reserve:
            self.shared_pool.release_borrow(sandbox_id)
        completed_wall = time.time()
        after_local, after_shared = self._usage()
        return NumaBorrowRecord(
            sandbox_id=sandbox_id, cgroup=str(leaf), reserved_bytes=capacity_bytes,
            source_node=self.local_node if reserve else self.shared_node,
            target_node=target_node, started_unix_s=started_wall,
            completed_unix_s=completed_wall,
            service_seconds=max(0.0, time.monotonic() - started),
            local_bytes_before=before_local, local_bytes_after=after_local,
            shared_bytes_before=before_shared, shared_bytes_after=after_shared,
        )

    def borrow(self, sandbox_id: str, capacity_bytes: int) -> NumaBorrowRecord:
        with self._lock:
            if sandbox_id in self._borrowed:
                raise RuntimeError(f"sandbox is already using shared memory: {sandbox_id}")
            record = self._move(
                sandbox_id, capacity_bytes, target_node=self.shared_node, reserve=True,
            )
            self._borrowed[sandbox_id] = (Path(record.cgroup), capacity_bytes)
            return record

    def pin_local(self, sandbox_id: str) -> None:
        """Pin after create/restore, before the worker releases further work.

        Cube's VM boot/resume precedes this hook. Autonomous guest activity in
        that interval may already have run under the parent's allowed set.
        """
        with self._lock:
            if sandbox_id in self._borrowed:
                raise RuntimeError(f"borrowed sandbox cannot be newly pinned: {sandbox_id}")
            leaf = self._leaf_for(sandbox_id)
            if self.cpus is not None:
                self.writer(leaf / "cpuset.cpus", self.cpus)
                self._verify_cpus(leaf)
            self.writer(leaf / "cpuset.mems", str(self.local_node))
            effective = (leaf / "cpuset.mems.effective").read_text(encoding="ascii").strip()
            if effective != str(self.local_node):
                raise RuntimeError(
                    f"VM LOCAL pin did not take effect: requested {self.local_node}, got {effective}"
                )
            self.placements.append({
                "sandbox_id": sandbox_id, "node_id": self.node_id,
                "numa_node": self.local_node, "cpus": self.cpus,
                "effective_mems": effective,
                "effective_cpus": (leaf / "cpuset.cpus.effective").read_text().strip()
                    if self.cpus is not None else None,
                "cgroup": str(leaf), "verified_unix_s": time.time(),
                "scope": "after_create_or_restore_before_worker_dispatch",
            })

    def _verify_cpus(self, leaf: Path) -> None:
        if self.cpus is None:
            return
        from .topology import parse_cpu_list
        effective = (leaf / "cpuset.cpus.effective").read_text().strip()
        if parse_cpu_list(effective) != parse_cpu_list(self.cpus):
            raise RuntimeError(f"VM CPU pin did not take effect: requested {self.cpus}, got {effective}")

    def return_local(self, sandbox_id: str) -> NumaBorrowRecord:
        with self._lock:
            try:
                _, capacity_bytes = self._borrowed[sandbox_id]
            except KeyError as exc:
                raise RuntimeError(f"sandbox is not using shared memory: {sandbox_id}") from exc
            record = self._move(
                sandbox_id, capacity_bytes, target_node=self.local_node, reserve=False,
            )
            self._borrowed.pop(sandbox_id)
            return record

    def release_destroyed(self, sandbox_id: str) -> None:
        """Release accounting only after CubeSandbox proved the VM absent."""
        with self._lock:
            if self._borrowed.pop(sandbox_id, None) is not None:
                self.shared_pool.release_borrow(sandbox_id)

    def borrowed(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._borrowed))
