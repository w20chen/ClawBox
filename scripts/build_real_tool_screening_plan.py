"""Build the real-tool screening plan from already prepared pre-tool images."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(r"C:\Users\29068\Desktop\ClawBox")
OUT = ROOT / ".artifacts" / "llc-placement-discovery-20260914" / "real_tool_screening_plan.json"
CACHE = ROOT / ".artifacts" / "llc-placement-discovery-20260914" / "cache_pressure_pretool_plans.json"
SWEEP = ROOT / ".artifacts" / "llc-placement-discovery-20260914" / "swe_runner_plan.json"


def main() -> None:
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    sweep = json.loads(SWEEP.read_text(encoding="utf-8"))
    by_name = {x["name"]: x for x in cache["targets"]}
    by_name.update(sweep["targets"])
    names = ["scim2-flake8", "django-read-migrations", "hyp3-pytest-suite"]
    targets = {name: by_name[name] for name in names}

    # Keep the live sysfs-derived topology and the four-way rotation used by
    # the existing runner. Four repeats are intentional: three would leave
    # one same-slice/domain variant untested.
    topology = {
        "source": "live sysfs cache/index3/shared_cpu_list, cluster_cpus_list and thread_siblings_list on kunpeng",
        "numa_node": 0,
        "linux_pool": [8, 10, 12, 14],
        "physical_cpus_by_slice": {
            "S0": [8, 10, 12, 14],
            "S1": [16, 18, 20, 22],
            "S2": [24, 26, 28, 30],
            "S3": [32, 34, 36, 38],
        },
        "smt_siblings_used": False,
    }
    variants = {
        "same-slice": [
            {"id": "S0", "workers": [[8], [10], [12], [14]]},
            {"id": "S1", "workers": [[16], [18], [20], [22]]},
            {"id": "S2", "workers": [[24], [26], [28], [30]]},
            {"id": "S3", "workers": [[32], [34], [36], [38]]},
        ],
        "2-slice": [
            {"id": "S0+S1", "workers": [[8], [10], [16], [18]]},
            {"id": "S1+S2", "workers": [[16], [18], [24], [26]]},
            {"id": "S2+S3", "workers": [[24], [26], [32], [34]]},
            {"id": "S3+S0", "workers": [[32], [34], [8], [10]]},
        ],
        "4-slice": [
            {"id": "core0", "workers": [[8], [16], [24], [32]]},
            {"id": "core1", "workers": [[10], [18], [26], [34]]},
            {"id": "core2", "workers": [[12], [20], [28], [36]]},
            {"id": "core3", "workers": [[14], [22], [30], [38]]},
        ],
    }
    payload = {
        "schema": "llc-real-tool-screening-v1",
        "host": "kunpeng",
        "numa_node": 0,
        "memory_bytes": 6 * 1024**3,
        "vcpus_per_replica": 1,
        "replicas_per_trial": 4,
        "repeats": 4,
        "random_seed": 20260915,
        "topology": topology,
        "placements": {
            name: {"workers": variants[name][0]["workers"], "description": "variant schedule in placement_variants"}
            for name in variants
        } | {"linux": {"workers": [topology["linux_pool"]], "description": "warmup only"}},
        "placement_variants": variants,
        "targets": targets,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(OUT)
    for name, target in targets.items():
        print(name, target.get("duration_s"), target.get("llc_read_M_per_CPU_s"), target.get("llc_mpki"), target.get("ipc"))


if __name__ == "__main__":
    main()
