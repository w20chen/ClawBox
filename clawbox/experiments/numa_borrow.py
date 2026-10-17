"""Live VM borrowing from the shared NUMA pool.

CubeSandbox places each microVM in a descendant cgroup.  Rebinding that leaf's
``cpuset.mems`` rebinds allocation and asks cgroup v2 to migrate existing pages.
Migration may be incomplete; residency deltas also include faults and frees.
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
    reservation_released: bool = False
    reservation_reused: bool = False
    # Net resident changes include faults/frees; they are not migration traffic.
    measurement_scope: str = "vm_cgroup_resident_delta"
    migrated_bytes: int | None = None
    effective_mems_before: str | None = None
    effective_mems_after: str | None = None
    charged_bytes_before: int | None = None
    charged_bytes_after: int | None = None
    local_relief_bytes: int = 0
    local_growth_bytes: int = 0
    shared_growth_bytes: int = 0
    shared_reclaimed_bytes: int = 0
    physical_effect: str = "unclassified"


@dataclass(frozen=True, slots=True)
class NumaResidencyObservation:
    sandbox_id: str
    cgroup: str
    effective_mems: str
    local_bytes: int
    shared_bytes: int
    other_numa_bytes: int
    unattributed_bytes: int
    charged_bytes: int
    reserved_bytes: int
    observed_unix_s: float


@dataclass(frozen=True, slots=True)
class _BorrowedAllocation:
    leaf: Path
    capacity_bytes: int
    parent_shared_bytes_before: int


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
                 writer: Callable[[Path, str], None] | None = None,
                 cleanup_timeout_seconds: float = 0) -> None:
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
        if cleanup_timeout_seconds < 0:
            raise ValueError("cleanup_timeout_seconds must be non-negative")
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self._lock = RLock()
        self._borrowed: dict[str, _BorrowedAllocation] = {}
        self._leaf_cache: dict[str, tuple[Path, Path]] = {}
        self._verified_leaves: dict[str, Path] = {}

    @staticmethod
    def _privileged_write(path: Path, value: str) -> None:
        limit = 5 if path.name == 'memory.reclaim' else 30
        try:
            completed = subprocess.run(
                ["sudo", "-n", "timeout", "--signal=KILL", str(limit), "tee", str(path)], input=value + "\n",
                text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                check=False, timeout=limit+1,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"timed out updating {path}") from exc
        if completed.returncode in (-9, 124, 137):
            raise RuntimeError(f"timed out or killed updating {path}: kernel write limit is {limit} seconds")
        if completed.returncode:
            raise RuntimeError(
                f"cannot update {path}: {completed.stderr.strip() or completed.returncode}"
            )

    def _leaf_for(self, sandbox_id: str) -> Path:
        verified = self._verified_leaves.get(sandbox_id)
        if verified is not None:
            try:
                verified.relative_to(self.cgroup_root)
                if (verified != self.cgroup_root
                        and (verified / 'cpuset.mems').exists()):
                    return verified
            except (FileNotFoundError, ValueError):
                pass
            self._verified_leaves.pop(sandbox_id, None)
        cached = self._leaf_cache.get(sandbox_id)
        if cached is not None:
            leaf, witness = cached
            try:
                unified = next(line.partition("0::")[2]
                               for line in (witness / 'cgroup').read_text(encoding='ascii').splitlines()
                               if line.startswith('0::'))
                if (sandbox_id.encode() in (witness / 'cmdline').read_bytes()
                        and (self.cgroup_mount / unified.lstrip('/')).resolve() == leaf
                        and (leaf / 'cpuset.mems').exists()):
                    return leaf
            except (FileNotFoundError, PermissionError, ProcessLookupError, StopIteration):
                pass
            self._leaf_cache.pop(sandbox_id, None)
        leaves: set[Path] = set()
        witnesses: dict[Path, Path] = {}
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
                witnesses[leaf] = entry
            except (FileNotFoundError, PermissionError, ProcessLookupError, StopIteration):
                continue
        if len(leaves) != 1:
            raise RuntimeError(
                f"expected one cgroup for sandbox {sandbox_id}, found {sorted(map(str, leaves))}"
            )
        leaf = next(iter(leaves))
        self._leaf_cache[sandbox_id] = (leaf, witnesses[leaf])
        return leaf

    def _observe_leaf(self, sandbox_id: str, leaf: Path,
                      reserved_bytes: int = 0) -> NumaResidencyObservation:
        resident, unattributed = read_numa_lru_bytes(leaf)
        charged = int((leaf / "memory.current").read_text(encoding="ascii").strip())
        return NumaResidencyObservation(
            sandbox_id=sandbox_id,
            cgroup=str(leaf),
            effective_mems=(leaf / "cpuset.mems.effective").read_text(
                encoding="ascii"
            ).strip(),
            local_bytes=resident.get(self.local_node, 0) + unattributed,
            shared_bytes=resident.get(self.shared_node, 0),
            other_numa_bytes=sum(
                value for node, value in resident.items()
                if node not in {self.local_node, self.shared_node}
            ),
            unattributed_bytes=unattributed,
            charged_bytes=charged,
            reserved_bytes=reserved_bytes,
            observed_unix_s=time.time(),
        )

    def _usage(self, leaf: Path) -> tuple[int, int]:
        observation = self._observe_leaf("", leaf)
        return observation.local_bytes, observation.shared_bytes

    def _shared_usage(self, cgroup: Path) -> int:
        resident, _unattributed = read_numa_lru_bytes(cgroup)
        return resident.get(self.shared_node, 0)

    def _move(self, sandbox_id: str, capacity_bytes: int, *, target_node: int,
              reserve: bool) -> NumaBorrowRecord:
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        leaf = self._leaf_for(sandbox_id)
        before = self._observe_leaf(sandbox_id, leaf, capacity_bytes)
        self._verify_cpus(leaf)
        reused = False
        if reserve:
            existing = self._borrowed.get(sandbox_id)
            if existing is not None:
                # Residual shared pages retain the full reservation after a
                # return. Reuse that capacity when this LOCAL VM needs it again.
                if before.effective_mems != str(self.local_node):
                    raise RuntimeError(f"sandbox is already using shared memory: {sandbox_id}")
                if existing.capacity_bytes != capacity_bytes or existing.leaf != leaf:
                    raise RuntimeError('retained borrow reservation does not match the VM')
                reused = True
            else:
                parent_shared_before = self._shared_usage(self.cgroup_root)
                self.shared_pool.reserve_borrow(sandbox_id, capacity_bytes)
                # Retain ownership even if the write or its verification fails.
                self._borrowed[sandbox_id] = _BorrowedAllocation(
                    leaf=leaf,
                    capacity_bytes=capacity_bytes,
                    parent_shared_bytes_before=parent_shared_before,
                )
        started_wall = time.time()
        started = time.monotonic()
        try:
            with self.shared_pool.transfer("rebind"):
                self.writer(leaf / "cpuset.mems", str(target_node))
            effective = (leaf / "cpuset.mems.effective").read_text(encoding="ascii").strip()
            if effective != str(target_node):
                raise RuntimeError(
                    f"VM cpuset rebind did not take effect: requested {target_node}, got {effective}"
                )
        except Exception:
            if reserve:
                try:
                    # A failed write can have taken effect. Undo allocation
                    # policy, then release only with physical evidence.
                    self.writer(leaf / "cpuset.mems", str(self.local_node))
                except Exception:
                    pass
                try:
                    local = (leaf / "cpuset.mems.effective").read_text().strip()
                    if local == str(self.local_node) and self._usage(leaf)[1] == 0:
                        self.shared_pool.release_borrow(sandbox_id)
                        self._borrowed.pop(sandbox_id)
                except Exception:
                    pass  # keep the reservation for verified destruction
            raise
        completed_wall = time.time()
        after = self._observe_leaf(sandbox_id, leaf, capacity_bytes)
        released = not reserve and after.shared_bytes == 0
        if released:
            self.shared_pool.release_borrow(sandbox_id)
        if reserve:
            physical_effect = (
                "allocation_policy_changed_with_observed_shared_growth"
                if after.shared_bytes > before.shared_bytes
                else "allocation_policy_changed_no_immediate_shared_growth"
            )
        else:
            physical_effect = (
                "local_policy_restored_reservation_released"
                if released else "local_policy_restored_residual_shared_residency"
            )
        return NumaBorrowRecord(
            sandbox_id=sandbox_id, cgroup=str(leaf), reserved_bytes=capacity_bytes,
            source_node=self.local_node if reserve else self.shared_node,
            target_node=target_node, started_unix_s=started_wall,
            completed_unix_s=completed_wall,
            service_seconds=max(0.0, time.monotonic() - started),
            local_bytes_before=before.local_bytes, local_bytes_after=after.local_bytes,
            shared_bytes_before=before.shared_bytes, shared_bytes_after=after.shared_bytes,
            reservation_released=released,
            reservation_reused=reused,
            effective_mems_before=before.effective_mems,
            effective_mems_after=after.effective_mems,
            charged_bytes_before=before.charged_bytes,
            charged_bytes_after=after.charged_bytes,
            local_relief_bytes=max(0, before.local_bytes - after.local_bytes),
            local_growth_bytes=max(0, after.local_bytes - before.local_bytes),
            shared_growth_bytes=max(0, after.shared_bytes - before.shared_bytes),
            shared_reclaimed_bytes=max(0, before.shared_bytes - after.shared_bytes),
            physical_effect=physical_effect,
        )

    def borrow(self, sandbox_id: str, capacity_bytes: int) -> NumaBorrowRecord:
        with self._lock:
            record = self._move(
                sandbox_id, capacity_bytes, target_node=self.shared_node, reserve=True,
            )
            return record

    def pin_local(self, sandbox_id: str, *, verified_cgroup: str | Path | None = None) -> None:
        """Pin after create/restore, before the worker releases further work.

        Cube's VM boot/resume precedes this hook. Autonomous guest activity in
        that interval may already have run under the parent's allowed set.
        """
        if verified_cgroup is not None:
            leaf = Path(verified_cgroup).resolve()
            try:
                leaf.relative_to(self.cgroup_root)
            except ValueError as exc:
                raise RuntimeError(f'verified VM cgroup is outside the configured domain: {leaf}') from exc
            if leaf == self.cgroup_root or not (leaf / 'cpuset.mems').exists():
                raise RuntimeError(f'verified VM cgroup is not a writable leaf cpuset: {leaf}')
            self._verified_leaves[sandbox_id] = leaf
        # Legacy/non-preallocated callers still use process discovery. Do not
        # hold the borrower lock during that read-only scan.
        leaf = self._leaf_for(sandbox_id)
        with self._lock:
            if sandbox_id in self._borrowed:
                raise RuntimeError(f"borrowed sandbox cannot be newly pinned: {sandbox_id}")
            if self.cpus is not None:
                from .topology import parse_cpu_list
                if parse_cpu_list((leaf/'cpuset.cpus.effective').read_text()) != parse_cpu_list(self.cpus):
                    self.writer(leaf / "cpuset.cpus", self.cpus)
                self._verify_cpus(leaf)
            if (leaf/'cpuset.mems.effective').read_text().strip() != str(self.local_node):
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
                capacity_bytes = self._borrowed[sandbox_id].capacity_bytes
            except KeyError as exc:
                raise RuntimeError(f"sandbox is not using shared memory: {sandbox_id}") from exc
            record = self._move(
                sandbox_id, capacity_bytes, target_node=self.local_node, reserve=False,
            )
            if record.reservation_released:
                self._borrowed.pop(sandbox_id)
            return record

    def release_destroyed(self, sandbox_id: str) -> None:
        """Release accounting only after CubeSandbox proved the VM absent."""
        deadline = time.monotonic() + self.cleanup_timeout_seconds
        while True:
            try:
                with self._lock:
                    entry = self._borrowed.get(sandbox_id)
                    if entry is None:
                        return
                    leaf = entry.leaf
                    if leaf.exists():
                        events = dict(
                            line.split()
                            for line in (leaf / "cgroup.events").read_text().splitlines()
                        )
                        # A destroyed VM can leave a small amount of reclaimable
                        # file cache charged to its now-empty cgroup. Ask the
                        # kernel to reclaim it, then keep residency authoritative.
                        if events.get("populated") == "0":
                            reclaim = leaf / "memory.reclaim"
                            if reclaim.exists():
                                try:
                                    shared = self._usage(leaf)[1]
                                    if shared:
                                        # memory.reclaim is root-owned on the
                                        # production cgroup.  It accepts a byte
                                        # amount (this kernel rejects "max").
                                        self.writer(reclaim, str(shared))
                                except (OSError, RuntimeError, ValueError):
                                    pass
                        if (
                            events.get("populated") != "0"
                            or self._usage(leaf)[1] != 0
                        ):
                            raise RuntimeError(
                                f"shared reservation retained: VM {sandbox_id} "
                                "still has processes or shared pages; reclaim its "
                                "cgroup and retry cleanup"
                            )
                    else:
                        parent_shared_after = self._shared_usage(self.cgroup_root)
                        if parent_shared_after > entry.parent_shared_bytes_before:
                            raise RuntimeError(
                                "shared reservation retained: removed VM cgroup "
                                f"{leaf} has no per-VM residency evidence and parent "
                                "shared residency remains above its pre-borrow baseline "
                                f"({parent_shared_after} > "
                                f"{entry.parent_shared_bytes_before})"
                            )
                    self.shared_pool.release_borrow(sandbox_id)
                    self._borrowed.pop(sandbox_id)
                    return
            except (FileNotFoundError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))

    def borrowed(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._borrowed))

    def residency(self, sandbox_id: str) -> NumaResidencyObservation:
        """Observe the VM's current physical placement without changing policy."""
        with self._lock:
            try:
                entry = self._borrowed[sandbox_id]
            except KeyError as exc:
                raise RuntimeError(
                    f"sandbox is not using shared memory: {sandbox_id}"
                ) from exc
            return self._observe_leaf(
                sandbox_id, entry.leaf, entry.capacity_bytes,
            )

    def observe_resident(self, sandbox_id: str) -> NumaResidencyObservation:
        """Observe a LOCAL candidate; do not mistake configured size for relief."""
        return self._observe_leaf(sandbox_id, self._leaf_for(sandbox_id))

    def allocates_shared(self, sandbox_id: str) -> bool:
        with self._lock:
            entry = self._borrowed.get(sandbox_id)
            return entry is not None and (
                entry.leaf / 'cpuset.mems.effective'
            ).read_text().strip() == str(self.shared_node)
