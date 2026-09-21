"""Build fresh-prefix and placement plans for the cache-pressure candidates."""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(r"C:\Users\29068\Desktop\ClawBox")
CALLS = ROOT / ".artifacts" / "placement-first-pass" / "calls.json"
PROFILE = ROOT / ".artifacts" / "llc-placement-discovery-20260914" / "cache_pressure_candidates.csv"
OUT = ROOT / ".artifacts" / "llc-placement-discovery-20260914" / "cache_pressure_pretool_plans.json"

TARGETS = [
    {
        "name": "django-read-migrations",
        "task_id": "983994178348e63426df",
        "span_id": "call_00_SU9Y2smeAwSV2cY6kEIe5625",
        "source_image": "swerebench/sweb.eval.x86_64.3yourmind_1776_django-migration-linter-113:latest",
        "workdir": "/testbed",
    },
    {
        "name": "mbed-test-mbed-program",
        "task_id": "d74471b12c62091c0318",
        "span_id": "call_00_3FvQHqNlLN3DIBMI3cfK9480",
        "source_image": "swerebench/sweb.eval.x86_64.armmbed_1776_mbed-tools-190:latest",
        "workdir": "/testbed",
    },
    {
        "name": "hyp3-pytest-suite",
        "task_id": "7b01d9a221b63055069b",
        "span_id": "call_00_nQklsDeM9aIKo45vSnLr7271",
        "source_image": "swerebench/sweb.eval.x86_64.asfhyp3_1776_hyp3-sdk-53:latest",
        "workdir": "/testbed",
    },
    {
        "name": "scim2-pytest-suite",
        "task_id": "1142b2bc2358172700d6",
        "span_id": "call_00_7LGawkQgaLpzEjA3ikrT8476",
        "source_image": "swerebench/sweb.eval.x86_64.15five_1776_scim2-filter-parser-13:latest",
        "workdir": "/testbed",
    },
    {
        "name": "greentea-pytest-filter",
        "task_id": "2e1c2a1ab8e44fd1d64f",
        "span_id": "call_00_DQSllCphLswiWwZZJZad8218",
        "source_image": "swerebench/sweb.eval.x86_64.armmbed_1776_greentea-263:latest",
        "workdir": "/testbed",
    },
    {
        "name": "clique-pytest-suite",
        "task_id": "3b8d616c5807a367daac",
        "span_id": "call_00_tllVLFQNW8KyZQugDQmq9266",
        "source_image": "swerebench/sweb.eval.x86_64.4degrees_1776_clique-26:latest",
        "workdir": "/testbed",
    },
    {
        "name": "ctl-pytest-plugin",
        "task_id": "b9431b7f8a32e1b942cd",
        "span_id": "call_00_d2mzCRY5F6BfivWlOk9e5238",
        "source_image": "swerebench/sweb.eval.x86_64.20c_1776_ctl-3:latest",
        "workdir": "/testbed",
    },
]


def prefix_for(calls: list[dict], task_id: str, sequence_no: int) -> list[dict]:
    rows = []
    for call in sorted((x for x in calls if x["task_id"] == task_id and x["sequence_no"] < sequence_no), key=lambda x: x["sequence_no"]):
        args = call.get("requested_args") or {}
        if args.get("command"):
            rows.append({"sequence_no": call["sequence_no"], "kind": "exec", "command": args["command"]})
        elif args.get("edits"):
            rows.append({"sequence_no": call["sequence_no"], "kind": "edit", "path": args.get("path"), "edits": args["edits"]})
        elif (args.get("input") or "").lstrip().startswith("*** Begin Patch"):
            rows.append({"sequence_no": call["sequence_no"], "kind": "patch", "input": args["input"]})
        elif args.get("content") is not None:
            rows.append({"sequence_no": call["sequence_no"], "kind": "write", "path": args.get("path"), "content": args["content"]})
    return rows


def main() -> None:
    calls = json.loads(CALLS.read_text(encoding="utf-8"))
    by_span = {x["span_id"]: x for x in calls}
    profiles = {}
    with PROFILE.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            profiles[row["span_id"]] = row

    targets = []
    for spec in TARGETS:
        call = by_span[spec["span_id"]]
        profile = profiles[spec["span_id"]]
        targets.append({
            "name": spec["name"],
            "task_id": spec["task_id"],
            "trace_id": call.get("trace_id", ""),
            "span_id": spec["span_id"],
            "sequence_no": call["sequence_no"],
            "repo": call["repo"],
            "source_image": spec["source_image"],
            "checkpoint_image": f"placement-cache-{spec['name']}-pretool:20260914",
            "workdir": spec["workdir"],
            "command": call["command"],
            "expected_exit_code": call["exit_code"] if call.get("exit_code") is not None else 0,
            "expected_stdout_tail": "",
            "duration_s": float(profile["duration_s"]),
            "cpu_active_ratio": float(profile["cpu_active_ratio"]),
            "llc_read_M_per_CPU_s": float(profile["llc_read_M_per_CPU_s"]),
            "llc_miss_M_per_CPU_s": float(profile["llc_miss_M_per_CPU_s"]),
            "H_M_per_CPU_s": float(profile["H_M_per_CPU_s"]),
            "llc_miss_rate": float(profile["llc_miss_rate"]),
            "llc_mpki": float(profile["llc_mpki"]),
            "ipc": float(profile["ipc"]),
            "prefix": prefix_for(calls, spec["task_id"], call["sequence_no"]),
        })

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
        "schema": "llc-cache-pressure-placement-v2",
        "host": "kunpeng",
        "numa_node": 0,
        "memory_bytes": 6 * 1024**3,
        "vcpus_per_replica": 1,
        "replicas_per_trial": 4,
        "repeats": 4,
        "random_seed": 20260914,
        "topology": topology,
        "placements": {
            "same-slice": {"workers": variants["same-slice"][0]["workers"], "description": "variant schedule in placement_variants"},
            "2-slice": {"workers": variants["2-slice"][0]["workers"], "description": "variant schedule in placement_variants"},
            "4-slice": {"workers": variants["4-slice"][0]["workers"], "description": "variant schedule in placement_variants"},
            "linux": {"workers": [[8, 10, 12, 14]], "description": "warmup only"},
        },
        "placement_variants": variants,
        "targets": targets,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(OUT)
    for target in targets:
        print(target["name"], "seq", target["sequence_no"], "prefix", len(target["prefix"]), "MPKI", target["llc_mpki"], "IPC", target["ipc"])


if __name__ == "__main__":
    main()
