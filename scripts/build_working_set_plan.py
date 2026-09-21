"""Build the controlled working-set plan from live Kunpeng topology data."""

from __future__ import annotations

import json
import sys
from pathlib import Path


SIZES_STEPS = {
    # The first calibration pass showed that the smallest case was just
    # under the requested 10 s under four-way load.  These values add a
    # conservative ~35% runtime margin while preserving the same access
    # pattern and working-set sizes.
    1: 1_300_000_000,
    2: 810_000_000,
    4: 675_000_000,
    8: 595_000_000,
    16: 475_000_000,
    32: 380_000_000,
    64: 260_000_000,
    128: 180_000_000,
    256: 160_000_000,
}


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: build_working_set_plan.py TOPOLOGY_JSON CHECKPOINT_IMAGE CHECKPOINT_ID")
    topology = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    checkpoint_image = sys.argv[2]
    checkpoint_id = sys.argv[3]
    slices = topology["physical_cpus_by_slice"]
    names = list(slices)

    variants = {
        "same-slice": [
            {"id": name, "workers": [[cpu] for cpu in slices[name][:4]]}
            for name in names
        ],
        "2-slice": [],
        "4-slice": [],
    }
    for i in range(len(names)):
        left = slices[names[i]]
        right = slices[names[(i + 1) % len(names)]]
        variants["2-slice"].append({
            "id": f"{names[i]}+{names[(i + 1) % len(names)]}",
            "workers": [[left[0]], [left[1]], [right[0]], [right[1]]],
        })
    for slot in range(4):
        variants["4-slice"].append({
            "id": f"core{slot}",
            "workers": [[slices[name][slot]] for name in names],
        })

    targets = {}
    for size_mib, steps in SIZES_STEPS.items():
        name = f"pointer-chase-{size_mib}MiB"
        targets[name] = {
            "name": name,
            "size_mib": size_mib,
            "steps": steps,
            "duration_s": 10.0,
            "band": "controlled-working-set",
            "predicted_rate": 0.0,
            "checkpoint_image": checkpoint_image,
            "checkpoint_image_id": checkpoint_id,
            "command": f"/opt/pointer_chase {size_mib} {steps} 305419896",
            "workdir": "/app",
            "expected_exit_code": 0,
            "expected_stdout_tail": "",
        }

    # Negative/control case: sequentially sweep a buffer well above the
    # selected LLC domain.  It is kept in the same checkpoint image and
    # runner so CPU, NUMA, memory, and PMU collection remain identical.
    targets["sequential-stream-512MiB"] = {
        "name": "sequential-stream-512MiB",
        "size_mib": 512,
        "passes": 330,
        "duration_s": 10.0,
        "band": "controlled-streaming-control",
        "predicted_rate": 0.0,
        "checkpoint_image": checkpoint_image,
        "checkpoint_image_id": checkpoint_id,
        "command": "/opt/sequential_stream 512 330",
        "workdir": "/app",
        "expected_exit_code": 0,
        "expected_stdout_tail": "",
    }

    payload = {
        "schema": "llc-controlled-working-set-v1",
        "host": "kunpeng",
        "numa_node": topology["numa_node"],
        "memory_bytes": 6 * 1024**3,
        "vcpus_per_replica": 1,
        "replicas_per_trial": 4,
        "repeats": 4,
        "random_seed": 20260915,
        "topology": topology,
        "placements": {
            **{
                key: {"workers": value[0]["workers"], "description": "rotated in placement_variants"}
                for key, value in variants.items()
            },
            "linux": {"workers": [topology["linux_pool"]], "description": "warmup only"},
        },
        "placement_variants": variants,
        "targets": targets,
    }
    Path("working_set_plan.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"targets": list(targets), "variants": variants}, indent=2))


if __name__ == "__main__":
    main()
