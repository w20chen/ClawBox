#!/usr/bin/env python3
"""Set NUMA/CPU policy without depending on a host numactl binary."""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import platform
from pathlib import Path


_SET_MEMPOLICY_NR = {"aarch64": 237, "x86_64": 238}
_MPOL_BIND = 2


def parse_cpu_set(value: str) -> set[int]:
    cpus: set[int] = set()
    for part in value.split(","):
        bounds = part.split("-", 1)
        first = int(bounds[0])
        last = int(bounds[-1])
        if first < 0 or last < first:
            raise ValueError(f"invalid CPU set component: {part!r}")
        cpus.update(range(first, last + 1))
    if not cpus:
        raise ValueError("CPU set cannot be empty")
    return cpus


def bind_memory(node: int) -> None:
    machine = platform.machine().lower()
    syscall_number = _SET_MEMPOLICY_NR.get(machine)
    if syscall_number is None:
        raise OSError(errno.ENOSYS, f"set_mempolicy unsupported on {machine}")
    word_bits = ctypes.sizeof(ctypes.c_ulong) * 8
    if node < 0 or node >= word_bits:
        raise ValueError(f"NUMA node must be in [0, {word_bits})")
    mask = ctypes.c_ulong(1 << node)
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(
        ctypes.c_long(syscall_number), ctypes.c_int(_MPOL_BIND),
        ctypes.byref(mask), ctypes.c_ulong(word_bits),
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def join_cgroup(path: Path) -> None:
    """Join an existing cgroup before exec so all Firecracker memory is charged."""
    resolved = path.resolve(strict=True)
    controllers = resolved / "cgroup.procs"
    if not controllers.is_file():
        raise ValueError(f"not a cgroup v2 directory: {resolved}")
    controllers.write_text(f"{os.getpid()}\n", encoding="ascii")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--numa-node", required=True, type=int)
    parser.add_argument("--cpu-set", required=True)
    parser.add_argument("--cgroup", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    bind_memory(args.numa_node)
    os.sched_setaffinity(0, parse_cpu_set(args.cpu_set))
    if args.cgroup is not None:
        join_cgroup(args.cgroup)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
