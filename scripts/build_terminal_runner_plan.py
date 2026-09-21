"""Create the v1 runner plan for the three fresh TerminalBench checkpoints."""

from __future__ import annotations

import json
from pathlib import Path


SRC = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\llc-placement-discovery-20260914\terminal_pretool_plans.json")
OUT = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\llc-placement-discovery-20260914\terminal_runner_plan.json")

CHECKPOINTS = {
    "3d-model-format-legacy": {
        "image": "placement-terminal-a-pretool:20260914",
        "image_id": "sha256:e781927995a3fcd5e3a4bea6d8e468c46490f7f8c895fae8d6ee1d206fb21a04",
    },
    "accelerate-maximal-square": {
        "image": "placement-terminal-b-pretool:20260914",
        "image_id": "sha256:68fc1e11aef4111870a2723bf2ffdb600ef93a34f9b62bc27e055f979f7f1b69",
    },
    "blind-maze-explorer-algorithm": {
        "image": "placement-terminal-c-pretool:20260914",
        "image_id": "sha256:68dc9ad388c22e45c38defe92ee84d8d855f6c40bd516f1e9e2d8d9590ec33c9",
    },
}


def main() -> None:
    source = json.loads(SRC.read_text(encoding="utf-8"))
    targets = {}
    for item in source["targets"]:
        checkpoint = CHECKPOINTS[item["name"]]
        targets[item["name"]] = {
            "trace": item["trace"],
            "span": item["span"],
            "duration_s": item["duration_s"],
            "llc_read_M_per_CPU_s": item["llc_read_M_per_CPU_s"],
            "llc_miss_M_per_CPU_s": item["llc_miss_M_per_CPU_s"],
            "H_M_per_CPU_s": item["H_M_per_CPU_s"],
            "predicted_rate": item["H_M_per_CPU_s"],
            "band": "terminalbench-pmu-reliable",
            "checkpoint_image": checkpoint["image"],
            "checkpoint_image_id": checkpoint["image_id"],
            "command": item["command"],
            "workdir": item["workdir"],
            "expected_exit_code": item["expected_exit_code"],
            "expected_stdout_sha256": item["expected_stdout_sha256"],
            "expected_stdout_tail": item["expected_stdout_tail"],
        }

    payload = {
        "schema": "placement-replica-terminal-v1",
        "host": "kunpeng",
        "numa_node": 0,
        "memory_bytes": 6 * 1024**3,
        "vcpus_per_replica": 1,
        "replicas_per_trial": 4,
        "repeats": 3,
        "random_seed": 20260914,
        "topology": {
            "source": "sysfs cache/index3/shared_cpu_list and cluster_cpus_list on kunpeng",
            "linux_pool": [8, 10, 12, 14],
            "no_smt_siblings": True,
            "slices": {
                "S0": {"cpus": [8, 10, 12, 14], "cluster_cpus": [8, 9, 10, 11, 12, 13, 14, 15]},
                "S1": {"cpus": [16, 20], "cluster_cpus": [16, 17, 18, 19, 20, 21, 22, 23]},
                "S2": {"cpus": [24], "cluster_cpus": [24, 25, 26, 27, 28, 29, 30, 31]},
                "S3": {"cpus": [32], "cluster_cpus": [32, 33, 34, 35, 36, 37, 38, 39]},
            },
        },
        "placements": {
            "same-slice": {"workers": [[8], [10], [12], [14]], "description": "4x in S0"},
            "2-slice": {"workers": [[8, 10], [8, 10], [16, 20], [16, 20]], "description": "2+2 across S0/S1"},
            "4-slice": {"workers": [[8], [16], [24], [32]], "description": "1+1+1+1 across S0/S1/S2/S3"},
            "linux": {"workers": [[8, 10, 12, 14]], "description": "warmup only"},
        },
        "targets": targets,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
