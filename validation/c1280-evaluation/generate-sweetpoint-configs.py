from __future__ import annotations

from copy import deepcopy
from pathlib import Path, PurePosixPath

import yaml


ROOT = Path(__file__).resolve().parent
BASE = ROOT / "supplement-run-20261018" / "320-full.yaml"
OUT = ROOT / "sweetpoint-run-20261018"

REMOTE_ROOT = PurePosixPath("/home/weitianc/clawbox-sweetpoint-20261018/source")
TRACE = REMOTE_ROOT / "examples/traces/sqlglot-4528-workspace-replay-v2.jsonl"

ARM_OVERRIDES = {
    "full": {},
    "aoff": {"admission.tool": "off"},
    "ioff": {"idle_reclaim.policy": "off"},
    "boff": {"shared_memory.borrow": "off"},
    "os": {
        "deployment.mode": "os-default",
        "admission.tool": "off",
        "runtime.memory_protection": False,
        "runtime.cpu_release_boost": False,
        "runtime.proactive_memory_reclaim": False,
        "placement.policy": "os_default",
        "idle_reclaim.policy": "off",
        "shared_memory.borrow": "off",
        "coordination.action_selection": "reclaim_first",
    },
}

POINTS = {
    "balanced": (160, 192),
    "tighter": (144, 176),
    "relaxed": (176, 208),
}


def make_spec(point: str, arm: str) -> dict:
    low_gib, high_gib = POINTS[point]
    spec = deepcopy(yaml.safe_load(BASE.read_text(encoding="utf-8")))

    spec["experiment_id"] = f"sweet-sqlglot-c320-{point}-{arm}-c0-20261018"

    resources = spec["environment"]["resources"]
    resources.update(
        local_memory_capacity_mib=928 * 1024,
        local_memory_low_watermark_mib=low_gib * 1024,
        local_memory_high_watermark_mib=high_gib * 1024,
        shared_memory_borrow_limit_mib=64 * 1024,
        pool_memory_budget_mib=high_gib * 1024,
        warm_memory_capacity_mib=256 * 1024,
    )
    for node in resources["compute_nodes"]:
        node.update(
            memory_capacity_mib=464 * 1024,
            low_watermark_mib=(low_gib // 2) * 1024,
            high_watermark_mib=(high_gib // 2) * 1024,
        )

    case = spec["workload"]["cases"][0]
    case.update(
        source_reference=str(TRACE),
        replay_trace_reference=str(TRACE),
        metadata={"purpose": "sweet-point screening with paced c320 arrivals"},
    )
    spec["workload"].update(input=str(TRACE), repetitions=1, session_assignment="single_case")

    placement = spec["mechanisms"]["placement"]
    placement["tool_call_cpu_policy"] = "c0"

    spec["comparison"]["arms"] = [
        {"name": arm, "override": ARM_OVERRIDES[arm]},
    ]
    spec["execution"].update(
        concurrency_levels=[320],
        random_seed=20261018,
        arm_timeout_seconds=2700,
        arm_finalization_timeout_seconds=120,
        command_timeout_seconds=600,
        arrival_schedule="fixed_stagger",
        stagger_interval_seconds=5.0,
        randomized_order=False,
    )
    return spec


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for point in POINTS:
        for arm in ARM_OVERRIDES:
            path = OUT / f"c320-{point}-{arm}-c0.yaml"
            path.write_text(
                yaml.safe_dump(make_spec(point, arm), sort_keys=False, width=100),
                encoding="utf-8",
            )


if __name__ == "__main__":
    main()
