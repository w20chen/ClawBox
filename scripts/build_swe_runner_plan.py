"""Merge current topology/placements with independently verified SWE checkpoints."""

from __future__ import annotations

import csv
import json
from pathlib import Path


BASE = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\llc-placement-discovery-20260914\terminal_runner_plan.json")
HEAVY = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\placement-replica-heavy-candidates\candidates.json")
DISCOVERY = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\llc-placement-discovery-20260914\discovered_candidates.csv")
CALLS = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\placement-first-pass\calls.json")
OUT = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\llc-placement-discovery-20260914\swe_runner_plan.json")
CHECKPOINT_IDS = {
    "scim2": "sha256:3f9eeb31deb3b921ee59faa9ba0d839468fb7d9eb0a6c09563ed1767ba76518e",
    "spectree": "sha256:40d0a9078da791a0ded07d6e37fa4cda46606966cfaa2199dfeec033d018ff82",
}


def main() -> None:
    base = json.loads(BASE.read_text(encoding="utf-8"))
    historical = json.loads(HEAVY.read_text(encoding="utf-8"))["targets"]
    calls = {(x["task_id"], x["sequence_no"]): x for x in json.loads(CALLS.read_text(encoding="utf-8"))}
    discovered = {}
    with DISCOVERY.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            discovered[(row["task_id"], row["span_id"])] = row

    targets = {}
    for name, key in [("scim2-flake8", "scim2"), ("spectree-parse-params", "spectree")]:
        old = historical[key]
        call = calls[(old["task_id"], old["sequence_no"])]
        row = discovered[(old["task_id"], call["span_id"])]
        targets[name] = {
            "trace": call["trace_id"],
            "span": call["span_id"],
            "task_id": old["task_id"],
            "sequence_no": old["sequence_no"],
            "repo": old["repo"],
            "duration_s": float(row["duration_s"]),
            "llc_read_M_per_CPU_s": float(row["llc_read_M_per_CPU_s"]),
            "llc_miss_M_per_CPU_s": float(row["llc_miss_M_per_CPU_s"]),
            "H_M_per_CPU_s": float(row["H_M_per_CPU_s"]),
            "predicted_rate": float(row["H_M_per_CPU_s"]),
            "band": "swe-pmu-reliable",
            "checkpoint_image": f"placement-replica-{key}-before-{old['sequence_no']}:20260914",
            "checkpoint_image_id": CHECKPOINT_IDS[key],
            "command": old["command"],
            "workdir": "/workspace",
            "expected_exit_code": old["expected_exit_code"],
            "expected_stdout_sha256": old.get("expected_stdout_sha256"),
            "expected_stdout_tail": old.get("expected_stdout_tail", ""),
        }

    payload = {
        "schema": "placement-replica-swe-v1",
        "host": base["host"],
        "numa_node": base["numa_node"],
        "memory_bytes": base["memory_bytes"],
        "vcpus_per_replica": base["vcpus_per_replica"],
        "replicas_per_trial": base["replicas_per_trial"],
        "repeats": base["repeats"],
        "random_seed": base["random_seed"],
        "topology": base["topology"],
        "placements": base["placements"],
        "targets": targets,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
