from __future__ import annotations

import argparse
import json
from pathlib import Path

from .baselines import BASELINES
from .spec import expand_matrix, load_experiment, spec_digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and plan ClawBox experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan"):
        command = commands.add_parser(name)
        command.add_argument("experiment", type=Path)
    commands.add_parser("list-baselines")
    args = parser.parse_args(argv)
    if args.command == "list-baselines":
        print(json.dumps({
            name: {
                "admission": baseline.admission_policy.value,
                "reclamation": baseline.reclamation_policy.value,
                "eviction": baseline.eviction_policy.value,
                "restore": baseline.restore_policy.value,
                "implementation_status": baseline.implementation_status,
            }
            for name, baseline in BASELINES.items()
        }, indent=2, sort_keys=True))
        return 0
    try:
        spec = load_experiment(args.experiment)
        arms = expand_matrix(spec)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    payload: dict[str, object] = {
        "valid": True, "spec_digest": spec_digest(spec), "arm_count": len(arms),
    }
    if args.command == "plan":
        payload["arms"] = [arm.model_dump(mode="json") for arm in arms]
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
