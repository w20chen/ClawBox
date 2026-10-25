"""Move a live VM's NUMA policy once per process, retaining its memory cgroup.

Run by the borrower under sudo. Temporary cpuset-only threaded children share
the Cube-owned parent's memory domain and are removed before returning.
"""
from __future__ import annotations

import json
import ctypes
import os
from pathlib import Path
import sys
import subprocess
import time
import uuid


def _write(path: Path, value: str | int) -> None:
    path.write_text(str(value) + "\n", encoding="ascii")


def _read(path: Path) -> str:
    return path.read_text(encoding="ascii").strip()


def _native_numa_control(pid: int, action: str) -> None:
    """Reuse the running CubeShim binary and its existing Task.Update socket."""
    process = Path("/proc") / str(pid)
    args = (process / "cmdline").read_bytes().decode().split("\0")
    try:
        socket = args[args.index("-socket") + 1]
        sandbox_id = args[args.index("-id") + 1]
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"cannot identify CubeShim control socket for PID {pid}") from exc
    result = subprocess.run(
        [str(process / "exe"), "--clawbox-numa-control", socket, sandbox_id, action],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Cube NUMA {action} failed: {result.stderr.strip()}; "
            "install the numa-migration-pause Shim patch on an idle host"
        )


def _migrate_pages(pid: int, source: str, target: int) -> int:
    """Move existing pages without holding the cgroup attachment lock.

    The subsequent cpuset attachment still enforces policy and migrates any
    residual/new pages. A positive return is residual pages, not an error or
    proof that all VM memory moved. The caller retains its full reservation.
    """
    try:
        lib = ctypes.CDLL("libnuma.so.1", use_errno=True)
    except OSError as exc:
        raise RuntimeError("NUMA borrowing requires libnuma; install the host numactl package") from exc
    lib.numa_parse_nodestring_all.argtypes = [ctypes.c_char_p]
    lib.numa_parse_nodestring_all.restype = ctypes.c_void_p
    lib.numa_migrate_pages.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
    lib.numa_migrate_pages.restype = ctypes.c_int
    lib.numa_bitmask_free.argtypes = [ctypes.c_void_p]
    masks = []
    try:
        for nodes in (source, str(target)):
            mask = lib.numa_parse_nodestring_all(nodes.encode("ascii"))
            if not mask:
                raise ValueError(f"cannot parse NUMA nodes: {nodes}")
            masks.append(mask)
        result = lib.numa_migrate_pages(pid, *masks)
        if result < 0:
            code = ctypes.get_errno()
            raise OSError(code, f"NUMA page migration failed: {os.strerror(code)}")
        return result
    finally:
        for mask in masks:
            lib.numa_bitmask_free(mask)


def rebind(leaf: Path, target: int) -> dict[str, float | int]:
    """Change placement without rewriting the cpuset of a populated group."""
    leaf = leaf.resolve(strict=True)
    previous = _read(leaf / "cpuset.mems.effective")
    started = time.monotonic()
    if previous == str(target):
        return {"processes": 0, "migration_seconds": 0.0,
                "service_seconds": time.monotonic() - started}
    pids = {int(pid) for pid in _read(leaf / "cgroup.procs").split()}
    if not pids:
        _write(leaf / "cpuset.mems", target)
        return {"processes": 0, "migration_seconds": 0.0,
                "service_seconds": time.monotonic() - started}
    if len(pids) != 1:
        raise RuntimeError(f"expected one Cube VMM process in {leaf}, found {sorted(pids)}")
    if _read(leaf / "cgroup.subtree_control"):
        raise RuntimeError(f"expected a Cube leaf without subtree controllers: {leaf}")
    pid = next(iter(pids))
    # Keep guest RAM slots detached until migration and cpuset changes finish.
    # Empty but registered slots still incur ARM KVM invalidation per page.
    # Cube pauses vCPUs/devices and saves dirty bits for incremental checkpoints.
    prepare_started = time.monotonic()
    _native_numa_control(pid, "prepare")
    native_prepare_seconds = time.monotonic() - prepare_started
    # cpuset_attach flushes a global ordered migration workqueue while holding
    # cgroup attachment serialization. Do bulk migration first so that unrelated
    # Cube create/restore/delete operations need not wait for this page copying.
    migration_started = time.monotonic()
    premigrate_remaining_pages = _migrate_pages(pid, previous, target)
    premigrate_seconds = time.monotonic() - migration_started
    cpuset_started = time.monotonic()
    task_dir = Path("/proc") / str(pid) / "task"
    affinities = {}
    for entry in task_dir.iterdir():
        try:
            affinities[int(entry.name)] = os.sched_getaffinity(int(entry.name))
        except ProcessLookupError:
            pass
    leader_affinity = affinities[pid]
    suffix = uuid.uuid4().hex[:12]
    source = leaf / f"clawbox-rebind-source-{suffix}"
    destination = leaf / f"clawbox-rebind-target-{suffix}"
    children: list[Path] = []
    enabled = False
    current = leaf
    migration_seconds = 0.0

    def restore_affinities() -> None:
        if not task_dir.exists():
            return
        for entry in task_dir.iterdir():
            tid = int(entry.name)
            try:
                os.sched_setaffinity(tid, affinities.get(tid, leader_affinity))
            except ProcessLookupError:
                pass

    try:
        source.mkdir()
        children.append(source)
        _write(source / "cgroup.type", "threaded")
        _write(leaf / "cgroup.subtree_control", "+cpuset")
        enabled = True
        cpus = _read(leaf / "cpuset.cpus.effective")
        _write(source / "cpuset.cpus", cpus)
        _write(source / "cpuset.mems", previous)
        # Identical effective masks: the kernel skips mm migration and CPU
        # affinity updates when attaching to this source child.
        _write(source / "cgroup.procs", pid)
        current = source
        _write(leaf / "cpuset.mems", f"{previous},{target}")
        destination.mkdir()
        children.append(destination)
        _write(destination / "cgroup.type", "threaded")
        _write(destination / "cpuset.cpus", cpus)
        _write(destination / "cpuset.mems", target)
        migration_started = time.monotonic()
        _write(destination / "cgroup.procs", pid)
        current = destination
        migration_seconds = time.monotonic() - migration_started
        # cpuset_attach resets thread affinities even when only mems changed.
        restore_affinities()
    finally:
        # The populated child keeps its explicit mask while the empty parent
        # changes. Attaching back with identical masks does not migrate again.
        # All allocations throughout remain charged to this parent domain.
        try:
            if current != leaf and task_dir.exists():
                _write(leaf / "cpuset.mems", _read(current / "cpuset.mems.effective"))
                _write(leaf / "cgroup.procs", pid)
            restore_affinities()
        finally:
            for child in reversed(children):
                child.rmdir()
            if enabled:
                _write(leaf / "cgroup.subtree_control", "-cpuset")
    if _read(leaf / "cpuset.mems.effective") != str(target):
        raise RuntimeError(f"NUMA rebind did not finish at node {target}: {leaf}")
    cpuset_seconds = time.monotonic() - cpuset_started
    # Failure intentionally leaves the VM paused and the reservation retained;
    # the owning experiment must destroy it, not resume unverified mappings.
    resume_started = time.monotonic()
    _native_numa_control(pid, "finish")
    return {"processes": 1, "migration_seconds": migration_seconds,
            "native_prepare_seconds": native_prepare_seconds,
            "native_resume_seconds": time.monotonic() - resume_started,
            "premigrate_seconds": premigrate_seconds,
            "premigrate_remaining_pages": premigrate_remaining_pages,
            "cpuset_seconds": cpuset_seconds,
            "service_seconds": time.monotonic() - started}


if __name__ == "__main__":
    if len(sys.argv) != 3 or not sys.argv[2].isdigit():
        raise SystemExit("usage: numa_rebind.py CUBE_VM_CGROUP TARGET_NUMA_NODE")
    print(json.dumps(rebind(Path(sys.argv[1]), int(sys.argv[2]))))
