#!/usr/bin/env python3
"""Read host NUMA/backing evidence for one explicitly identified experiment VM.

Run as root on the CubeSandbox host. Snapshot sampling touches only pages that
mincore reports already resident, so it does not populate the entire snapshot.
"""
import argparse
import ctypes
import json
import mmap
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--sandbox-id", required=True)
parser.add_argument("--snapshot", type=Path)
args = parser.parse_args()
inventory = {}
for proc in Path("/proc").glob("[0-9]*"):
    try:
        command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        status = (proc / "status").read_text()
        parent = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("PPid:"))
        inventory[int(proc.name)] = (proc, command, status, parent)
    except (OSError, ValueError):
        continue
selected = {pid for pid, (_, command, _, _) in inventory.items()
            if args.sandbox_id in command and "inspect-tier-pages" not in command}
while True:
    descendants = {pid for pid, (_, _, _, parent) in inventory.items() if parent in selected}
    if descendants <= selected:
        break
    selected.update(descendants)
processes = []
for pid in sorted(selected):
    proc, command, status, _ = inventory[pid]
    try:
        processes.append({"pid": pid, "command": command, "status": status,
                          "tiered_environment": [value.decode() for value in
                              (proc / "environ").read_bytes().split(b"\0")
                              if value.startswith((b"CUBE_RESTORE_PRIVATE_COPY=",
                                                   b"CLAWBOX_WARM_PREALLOCATE="))],
                          "cgroup": (proc / "cgroup").read_text(),
                          "numa_maps": (proc / "numa_maps").read_text()})
    except OSError:
        continue
report = {"sandbox_id": args.sandbox_id, "processes": processes}
report["cgroups"] = {}
for role, name in (("local", "sandbox"), ("storage", "cubelet")):
    group = Path("/sys/fs/cgroup/cube_sandbox") / name
    report["cgroups"][role] = {
        item: (group / item).read_text().strip()
        for item in ("memory.current", "memory.stat", "memory.numa_stat",
                     "memory.events", "memory.max", "cpuset.mems.effective")
        if (group / item).exists()
    }
if args.snapshot and args.snapshot.exists():
    stat = args.snapshot.stat()
    report["snapshot"] = {"path": str(args.snapshot), "logical_bytes": stat.st_size,
                          "allocated_bytes": stat.st_blocks * 512}
    if stat.st_size:
        with args.snapshot.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_COPY) as mapping:
            address = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
            pages = (stat.st_size + mmap.PAGESIZE - 1) // mmap.PAGESIZE
            vector = (ctypes.c_ubyte * pages)()
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.mincore(ctypes.c_void_p(address), ctypes.c_size_t(stat.st_size), vector):
                raise OSError(ctypes.get_errno(), "mincore failed")
            resident = 0
            for index in range(pages):
                if vector[index] & 1:
                    _ = mapping[index * mmap.PAGESIZE]
                    resident += 1
            report["snapshot"]["resident_pages_before_probe"] = resident
            report["snapshot"]["page_size"] = mmap.PAGESIZE
            report["snapshot"]["numa_maps"] = [
                line for line in Path("/proc/self/numa_maps").read_text().splitlines()
                if line.split()[0] == f"{address:x}"]
print(json.dumps(report))
