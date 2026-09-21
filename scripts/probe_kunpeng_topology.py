"""Probe the Kunpeng cache/sibling/NUMA mapping directly from sysfs."""

from __future__ import annotations

import json
import sys
from pathlib import Path


CPU_ROOT = Path("/sys/devices/system/cpu")
NODE_ROOT = Path("/sys/devices/system/node")


def expand(value: str) -> list[int]:
    result = []
    for part in value.strip().split(","):
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            result.extend(range(int(left), int(right) + 1))
        else:
            result.append(int(part))
    return sorted(result)


def main() -> None:
    requested = None
    if len(sys.argv) == 2:
        requested = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["physical_cpus_by_slice"]
    elif len(sys.argv) > 2:
        raise SystemExit("usage: probe_kunpeng_topology.py [CURRENT_SELECTED_SLICES_JSON]")
    node0 = expand((NODE_ROOT / "node0/cpulist").read_text())
    node0_set = set(node0)
    # Kunpeng exposes the LLC as one shared node-level cache in
    # cache/index3/shared_cpu_list.  The placement experiment's four domains
    # are the hardware cluster_cpu topology groups, so record both signals and
    # group workers by topology/cluster_cpus_list rather than CPU numbering.
    slices: dict[tuple[int, ...], dict] = {}
    shared_domains = set()
    for cpu in node0:
        cache = CPU_ROOT / f"cpu{cpu}/cache/index3"
        shared = tuple(expand((cache / "shared_cpu_list").read_text()))
        cluster = tuple(expand((CPU_ROOT / f"cpu{cpu}/topology/cluster_cpus_list").read_text()))
        siblings = tuple(expand((CPU_ROOT / f"cpu{cpu}/topology/thread_siblings_list").read_text()))
        if set(shared) - node0_set:
            raise RuntimeError(f"cache domain crosses NUMA node 0: cpu={cpu} shared={shared}")
        if set(cluster) - node0_set:
            raise RuntimeError(f"cluster domain crosses NUMA node 0: cpu={cpu} cluster={cluster}")
        shared_domains.add(shared)
        item = slices.setdefault(cluster, {"logical": list(cluster), "shared": list(shared), "siblings": set()})
        item["siblings"].add(siblings)

    if requested:
        selected = {}
        for name, physical in requested.items():
            groups = set()
            for cpu in physical:
                cluster = tuple(expand((CPU_ROOT / f"cpu{cpu}/topology/cluster_cpus_list").read_text()))
                groups.add(cluster)
            if len(groups) != 1:
                raise RuntimeError(f"requested {name} spans multiple sysfs clusters: {groups}")
            selected[next(iter(groups))] = name
        ordered = sorted(((logical, slices[logical]) for logical in selected), key=lambda item: selected[item[0]])
    else:
        ordered = sorted(slices.items(), key=lambda item: item[0][0])
    physical_by_slice = {}
    details = {}
    for index, (logical, item) in enumerate(ordered):
        name = selected.get(logical, f"S{index}") if requested else f"S{index}"
        sibling_groups = sorted(item["siblings"], key=lambda group: group[0])
        physical = [min(group) for group in sibling_groups]
        if len(physical) < 4:
            raise RuntimeError(f"slice {name} has fewer than four physical cores: {physical}")
        physical_by_slice[name] = physical
        details[name] = {
            "llc_shared_cpu_list": item["shared"],
            "cluster_cpus_list": list(logical),
            "thread_siblings": [list(group) for group in sibling_groups],
            "physical_cpus": physical,
        }

    payload = {
        "source": "live sysfs: node0/cpulist, cache/index3/shared_cpu_list, topology/cluster_cpus_list, thread_siblings_list; selected domains validated against current placement definition",
        "numa_node": 0,
        "linux_pool": physical_by_slice["S0"][:4],
        "physical_cpus_by_slice": physical_by_slice,
        "slices": details,
        "distinct_llc_shared_cpu_lists": [list(value) for value in sorted(shared_domains)],
        "smt_siblings_used": False,
    }
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
