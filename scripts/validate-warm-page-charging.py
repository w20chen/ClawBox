#!/usr/bin/env python3
"""Root-only, temporary 64 MiB probe of WARM charging outside LOCAL.

Creates two temporary cgroups and one temporary file; removes only those
objects. The writer is restricted to NUMA0, while the existing WARM tmpfs
mount supplies its NUMA1 policy. No VM or existing cgroup limit is changed.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--warm", required=True, type=Path)
args = parser.parse_args()
base = Path("/sys/fs/cgroup/cube_sandbox")
token = "clawbox-charge-probe-" + uuid.uuid4().hex[:10]
storage = base / (token + "-storage")
local = base / (token + "-local")
size = 64 * 1024 * 1024
fd, name = tempfile.mkstemp(prefix=token, dir=args.warm)
os.close(fd)
report = {"size_bytes": size, "passed": False}


def child(group, action):
    pid = os.fork()
    if pid == 0:
        try:
            (group / "cgroup.procs").write_text(str(os.getpid()))
            action()
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    if status:
        raise RuntimeError(f"probe child failed: {status}")


def allocate():
    with open(name, "r+b", buffering=0) as output:
        os.posix_fallocate(output.fileno(), 0, size)


def write_existing():
    with open(name, "r+b", buffering=0) as output:
        block = b"Q" * (1024 * 1024)
        for _ in range(64):
            output.write(block)


try:
    if "cpuset" not in (base / "cgroup.subtree_control").read_text().split():
        if (base / "cgroup.procs").read_text().strip():
            raise RuntimeError("cannot enable cpuset on a populated parent")
        (base / "cgroup.subtree_control").write_text("+cpuset")
    for group in (storage, local):
        group.mkdir()
        (group / "memory.max").write_text(str(128 * 1024 * 1024))
        (group / "memory.swap.max").write_text("0")
    (local / "cpuset.mems").write_text("0")
    child(storage, allocate)
    report["storage_after_allocate"] = int((storage / "memory.current").read_text())
    child(local, write_existing)
    report["storage_after_write"] = int((storage / "memory.current").read_text())
    report["local_after_write"] = int((local / "memory.current").read_text())
    report["storage_numa_stat"] = (storage / "memory.numa_stat").read_text()
    report["local_numa_stat"] = (local / "memory.numa_stat").read_text()
    report["local_mems"] = (local / "cpuset.mems.effective").read_text().strip()
    report["allocated_bytes"] = os.stat(name).st_blocks * 512
    assert report["storage_after_write"] >= size
    assert report["local_after_write"] < size // 4
    assert report["local_mems"] == "0"
    file_nodes = dict(field.split("=") for line in report["storage_numa_stat"].splitlines()
                      if line.startswith("file ") for field in line.split()[1:])
    assert int(file_nodes.get("N1", "0")) >= size
    report["passed"] = True
finally:
    Path(name).unlink(missing_ok=True)
    for group in (local, storage):
        if group.exists():
            group.rmdir()
    print(json.dumps(report, indent=2))
