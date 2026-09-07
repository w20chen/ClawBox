#!/usr/bin/env python3
"""Configure the idle standalone CubeSandbox VM cgroup for a tiered study.

Run as root only on the dedicated experiment deployment. Refuses a populated
VM subtree. Does not change Kubernetes cgroups or the Cubelet storage cgroup.
Re-run after deployment restart and before each formal arm (with no live VMs).
"""
import argparse
import errno
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--capacity-mib", type=int, default=65536)
parser.add_argument("--numa-node", type=int, default=0)
parser.add_argument("--reclaim-storage-cache", action="store_true")
args = parser.parse_args()
if args.capacity_mib <= 0 or args.numa_node < 0:
    parser.error("capacity must be positive and NUMA node non-negative")
base = Path("/sys/fs/cgroup/cube_sandbox")
group = base / "sandbox"
if "populated 0" not in (group / "cgroup.events").read_text().splitlines():
    raise SystemExit("refusing to change LOCAL limits: standalone VMs are still running")
cpus = (Path("/sys/devices/system/node") / f"node{args.numa_node}" / "cpulist").read_text().strip()
before = int((group / "memory.current").read_text())
for parent in (base, group):
    if "cpuset" not in (parent / "cgroup.subtree_control").read_text().split():
        (parent / "cgroup.subtree_control").write_text("+cpuset")
(group / "cpuset.mems").write_text(str(args.numa_node))
(group / "cpuset.cpus").write_text(cpus)
storage_group = base / "cubelet"
# Snapshot-copy work also represents work on the LOCAL host. The WARM mount
# keeps its explicit NUMA1 memory policy; only CPU affinity is constrained here.
(storage_group / "cpuset.cpus").write_text(cpus)
(group / "memory.swap.max").write_text("0")
(group / "memory.max").write_text(str(args.capacity_mib * 1024 * 1024))
reclaim_status = "not_needed"
if before:
    try:
        (group / "memory.reclaim").write_text(str(before))
        reclaim_status = "requested"
    except OSError as exc:
        if exc.errno != errno.EAGAIN:
            raise
        reclaim_status = "partial_EAGAIN"
storage_reclaim = "not_requested"
if args.reclaim_storage_cache:
    stats = dict(line.split() for line in (storage_group / "memory.stat").read_text().splitlines())
    reclaimable = max(0, int(stats.get("file", "0")) - int(stats.get("shmem", "0")))
    if reclaimable:
        try:
            (storage_group / "memory.reclaim").write_text(str(reclaimable))
            storage_reclaim = "requested"
        except OSError as exc:
            if exc.errno != errno.EAGAIN:
                raise
            storage_reclaim = "partial_EAGAIN"
print(json.dumps({
    "cgroup": str(group), "idle_before": True,
    "memory_before_bytes": before, "reclaim": reclaim_status,
    "storage_cache_reclaim": storage_reclaim,
    "storage_memory_current": (storage_group / "memory.current").read_text().strip(),
    **{name: (group / name).read_text().strip() for name in (
        "memory.current", "memory.max", "memory.swap.max", "memory.events",
        "cpuset.cpus.effective", "cpuset.mems.effective", "memory.stat")},
}, indent=2))
