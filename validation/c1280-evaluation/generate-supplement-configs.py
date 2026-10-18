from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent
BASE = ROOT / "actual-idle-matrix" / "actual-idle-c320-full.yaml"
OUT = ROOT / "supplement-run-20261018"

ARM_OVERRIDES = {
    "full": {},
    "aoff": {"admission.tool": "off"},
    "ioff": {"idle_reclaim.policy": "off"},
    "boff": {"shared_memory.borrow": "off"},
    "runtime-off": {
        "runtime.memory_protection": False,
        "runtime.cpu_release_boost": False,
        "runtime.proactive_memory_reclaim": False,
    },
    "round-robin": {"placement.policy": "round_robin"},
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


def make_spec(density: int, arm: str) -> dict:
    spec = deepcopy(yaml.safe_load(BASE.read_text(encoding="utf-8")))
    family = "m320" if density == 320 else "m640"
    low_gib, high_gib, timeout = (
        (384, 448, 960) if density == 320 else (640, 768, 1080)
    )
    resources = spec["environment"]["resources"]
    resources.update(
        local_memory_capacity_mib=928 * 1024,
        local_memory_low_watermark_mib=low_gib * 1024,
        local_memory_high_watermark_mib=high_gib * 1024,
        # Keep the same 992 GiB physical envelope in every arm.  An arm that
        # disables borrowing changes policy, not the host/cgroup allowance.
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

    spec["experiment_id"] = f"supplement-{family}-{arm}-20261018"
    spec["comparison"]["arms"] = [
        {"name": arm, "override": ARM_OVERRIDES[arm]}
    ]
    execution = spec["execution"]
    execution.update(
        concurrency_levels=[density],
        random_seed=20261018,
        arm_timeout_seconds=timeout,
        arm_finalization_timeout_seconds=120,
        arrival_schedule="fixed_stagger",
        stagger_interval_seconds=0.5,
        randomized_order=False,
    )
    return spec


def make_relaxed_320_spec(arm: str) -> dict:
    spec = make_spec(320, arm)
    resources = spec["environment"]["resources"]
    resources.update(
        local_memory_low_watermark_mib=448 * 1024,
        local_memory_high_watermark_mib=512 * 1024,
        pool_memory_budget_mib=512 * 1024,
    )
    for node in resources["compute_nodes"]:
        node.update(low_watermark_mib=224 * 1024, high_watermark_mib=256 * 1024)
    spec["experiment_id"] = f"supplement-m320-relaxed-{arm}-20261018"
    return spec


def make_paced_320_spec(arm: str) -> dict:
    spec = make_relaxed_320_spec(arm)
    spec["experiment_id"] = f"supplement-m320-paced-{arm}-20261018"
    spec["execution"].update(
        stagger_interval_seconds=1.5,
        arm_timeout_seconds=1200,
    )
    return spec


def make_sustainable_320_spec(arm: str) -> dict:
    spec = make_relaxed_320_spec(arm)
    spec["experiment_id"] = f"supplement-m320-sustainable-{arm}-20261018"
    spec["execution"].update(
        stagger_interval_seconds=2.5,
        arm_timeout_seconds=1800,
    )
    return spec


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for density in (320, 640):
        for arm in ARM_OVERRIDES:
            path = OUT / f"{density}-{arm}.yaml"
            path.write_text(
                yaml.safe_dump(make_spec(density, arm), sort_keys=False, width=100),
                encoding="utf-8",
            )
    for arm in ARM_OVERRIDES:
        path = OUT / f"320-relaxed-{arm}.yaml"
        path.write_text(
            yaml.safe_dump(make_relaxed_320_spec(arm), sort_keys=False, width=100),
            encoding="utf-8",
        )
        path = OUT / f"320-paced-{arm}.yaml"
        path.write_text(
            yaml.safe_dump(make_paced_320_spec(arm), sort_keys=False, width=100),
            encoding="utf-8",
        )
        path = OUT / f"320-sustainable-{arm}.yaml"
        path.write_text(
            yaml.safe_dump(make_sustainable_320_spec(arm), sort_keys=False, width=100),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
