#!/usr/bin/env python3
"""Configure the idle standalone CubeSandbox VM cgroup for a tiered study.

Run as root only on the dedicated experiment deployment. Refuses a populated
VM subtree. Does not change Kubernetes cgroups or the Cubelet storage cgroup.
Re-run after deployment restart and before each formal arm (with no live VMs).
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def bounded_reclaim(path: Path, amount: int, *, timeout: int = 5) -> str:
    """Request cgroup reclaim without allowing a kernel write to stall setup."""
    if amount <= 0:
        return "not_needed"
    program = r"""
import errno
from pathlib import Path
import sys
try:
    Path(sys.argv[1]).write_text(sys.argv[2])
except OSError as exc:
    if exc.errno == errno.EAGAIN:
        raise SystemExit(75)
    raise
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", program, str(path), str(amount)],
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return "timed_out"
    if result.returncode == 0:
        return "requested"
    if result.returncode == 75:
        return "partial_EAGAIN"
    raise RuntimeError(
        f"memory reclaim helper failed for {path} with exit {result.returncode}"
    )

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--capacity-mib", type=int, default=65536)
parser.add_argument("--numa-node", type=int, default=0)
parser.add_argument("--numa-nodes", help="comma-separated compute NUMA nodes")
parser.add_argument("--cpus", help="compute CPU subset; must belong to the selected nodes")
parser.add_argument("--shared-borrow-mib", type=int, default=0)
parser.add_argument("--shared-node", type=int)
parser.add_argument("--reclaim-storage-cache", action="store_true")
args = parser.parse_args()
if (args.capacity_mib <= 0 or args.numa_node < 0 or args.shared_borrow_mib < 0
        or (args.shared_borrow_mib and args.shared_node is None)):
    parser.error("capacity must be positive and NUMA node non-negative")
if args.shared_borrow_mib and args.shared_node == args.numa_node:
    parser.error("LOCAL and shared NUMA nodes must differ")
base = Path("/sys/fs/cgroup/cube_sandbox")
group = base / "sandbox"
if "populated 0" not in (group / "cgroup.events").read_text().splitlines():
    raise SystemExit("refusing to change LOCAL limits: standalone VMs are still running")
nodes = [int(n) for n in args.numa_nodes.split(",")] if args.numa_nodes else [args.numa_node]
if len(set(nodes)) != len(nodes) or any(n < 0 for n in nodes) or args.shared_node in nodes and args.shared_borrow_mib:
    parser.error("compute NUMA nodes must be unique, non-negative and distinct from shared")
def cpu_set(value):
    result = set()
    for item in value.split(","):
        ends = item.split("-")
        result.update(range(int(ends[0]), int(ends[-1]) + 1))
    return result
available = set()
for node in nodes:
    available.update(cpu_set((Path("/sys/devices/system/node") / f"node{node}" / "cpulist").read_text().strip()))
chosen = cpu_set(args.cpus) if args.cpus else available
if not chosen or not chosen <= available:
    parser.error("CPU subset must belong to the compute NUMA nodes")
cpus = ",".join(map(str, sorted(chosen)))
before = int((group / "memory.current").read_text())
for parent in (base, group):
    if "cpuset" not in (parent / "cgroup.subtree_control").read_text().split():
        (parent / "cgroup.subtree_control").write_text("+cpuset")
(group / "cpuset.mems").write_text(
    ",".join(map(str, sorted(set(nodes) | ({args.shared_node} if args.shared_node is not None else set()))))
)
(group / "cpuset.cpus").write_text(cpus)
storage_group = base / "cubelet"
# Snapshot-copy work also represents work on the LOCAL host. The WARM mount
# keeps its explicit shared-NUMA memory policy; only CPU affinity is constrained here.
(storage_group / "cpuset.cpus").write_text(cpus)
(group / "memory.swap.max").write_text("0")
(group / "memory.max").write_text(
    str((args.capacity_mib + args.shared_borrow_mib) * 1024 * 1024)
)
reclaim_status = bounded_reclaim(group / "memory.reclaim", before)
storage_reclaim = "not_requested"
if args.reclaim_storage_cache:
    stats = dict(line.split() for line in (storage_group / "memory.stat").read_text().splitlines())
    reclaimable = max(0, int(stats.get("file", "0")) - int(stats.get("shmem", "0")))
    if reclaimable:
        storage_reclaim = bounded_reclaim(
            storage_group / "memory.reclaim", reclaimable,
        )
print(json.dumps({
    "cgroup": str(group), "idle_before": True,
    "local_capacity_mib": args.capacity_mib,
    "shared_borrow_capacity_mib": args.shared_borrow_mib,
    "memory_before_bytes": before, "reclaim": reclaim_status,
    "storage_cache_reclaim": storage_reclaim,
    "storage_memory_current": (storage_group / "memory.current").read_text().strip(),
    **{name: (group / name).read_text().strip() for name in (
        "memory.current", "memory.max", "memory.swap.max", "memory.events",
        "cpuset.cpus.effective", "cpuset.mems.effective", "memory.stat")},
}, indent=2))
