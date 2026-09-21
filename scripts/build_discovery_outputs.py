"""Create the discovery/screening CSV shells and the current summary markdown."""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(r"C:\Users\29068\Desktop\ClawBox")
ART = ROOT / ".artifacts" / "llc-placement-discovery-20260914"


def main() -> None:
    plan = json.loads((ART / "real_tool_screening_plan.json").read_text(encoding="utf-8"))
    rows = []
    for name, target in plan["targets"].items():
        row = {"workload": name, "duration_s": target.get("duration_s"), "cpu_active_ratio": target.get("cpu_active_ratio"), "memory_footprint_proxy": "pending measured cgroup current/peak", "llc_read_M_per_CPU_s": target.get("llc_read_M_per_CPU_s"), "llc_miss_rate": target.get("llc_miss_rate"), "llc_mpki": target.get("llc_mpki"), "ipc": target.get("ipc"), "command": target.get("command"), "checkpoint_image": target.get("checkpoint_image"), "screening_status": "pending-host-recovery", "failure_reason": "Kunpeng 193.124.7.2:22 unreachable before trial start"}
        rows.append(row)
    with (ART / "real_tool_screening.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    with (ART / "validated_positive_tools.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["workload", "validation_status", "same_median_s", "four_median_s", "speedup", "four_wins", "reason"])
        writer.writeheader()
    summary = ART / "llc-placement-discovery-summary.md"
    summary.write_text("""# LLC placement discovery status\n\n## Current status\n\nThe controlled pointer-chasing calibration was intentionally stopped before its fourth rotation when the user requested an earlier transition to real Agent tools. The partial data is not a final calibration result.\n\nAt stop, 64 verified synthetic trials were recorded across 1--256 MiB. Partial same/4-slice median makespan ratios were: 1 MiB 0.958, 2 MiB 0.942, 4 MiB 1.168, 8 MiB 1.391 (2 rotations), 16 MiB 1.243 (2 rotations), 32 MiB 1.045, 64 MiB 1.017, 128 MiB 1.009, 256 MiB 1.000. These are descriptive only because the fourth domain rotation was incomplete.\n\nThe sequential 512 MiB streaming control at 330 passes reached approximately 10.2 s in its fastest placement during sanity testing; its raw sanity trial is retained separately.\n\n## Real tools\n\nThe real-tool screening plan contains scim2-flake8, django-read-migrations, and hyp3-pytest-suite. No real-tool trial started: Kunpeng became unreachable at 193.124.7.2:22 before the plan was transferred and the fresh real-tool root was created.\n\n## Interpretation\n\nNo strong-positive real Agent tool has been established in this run. The synthetic partial observations cannot distinguish a true cache-domain effect from CPU/runtime drift, so they must not be used as evidence for a placement scheduling rule.\n""", encoding="utf-8")


if __name__ == "__main__":
    main()
