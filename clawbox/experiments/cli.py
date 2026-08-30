"""Read-only inspection CLI for canonical experiment specifications."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .baselines import BASELINES
from .capabilities import validate_workflow
from .spec import expand_matrix, load_experiment


def _dump(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "resolve", "matrix"):
        item = commands.add_parser(command)
        item.add_argument("experiment", type=Path)
    commands.add_parser("list-baselines")
    args = parser.parse_args(argv)
    if args.command == "list-baselines":
        _dump({name: {"admission_policy": baseline.admission_policy,
                      "residency_policy": baseline.residency_policy,
                      "implementation_status": baseline.implementation_status}
               for name, baseline in BASELINES.items()})
        return 0
    try:
        spec = load_experiment(args.experiment)
        workflows = expand_matrix(spec, validate=False)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    assessments = [validate_workflow(workflow) for workflow in workflows]
    if args.command == "validate":
        _dump({"valid": all(item.implemented for item in assessments), "workflows": [
            {"workflow": workflow.model_dump(mode="json"), "capability": assessment.__dict__}
            for workflow, assessment in zip(workflows, assessments)
        ]})
        return 0 if all(item.implemented for item in assessments) else 2
    if not all(item.implemented for item in assessments):
        parser.error("; ".join(item.reason for item in assessments if not item.implemented))
    if args.command == "resolve":
        if len(workflows) != 1:
            parser.error("resolve requires exactly one workflow; use matrix to inspect a Cartesian expansion")
        _dump(workflows[0])
        return 0
    _dump([workflow.model_dump(mode="json") for workflow in workflows])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
