"""Standalone CubeSandbox experiment CLI."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from clawbox.experiments import expand_matrix, load_experiment, spec_digest


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--output-root", type=Path,
                      default=Path(os.getenv("CLAWBOX_OUTPUT_ROOT", "results")))
    commands = root.add_subparsers(dest="group", required=True)
    experiment = commands.add_parser("experiment")
    sub = experiment.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan", "run"):
        command = sub.add_parser(name)
        command.add_argument("spec", type=Path)
        if name == "run":
            command.add_argument("--run-id")
            command.add_argument("--attempt-id")
            command.add_argument("--owner-id")
    for name in ("status", "collect"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
    return root


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    try:
        if args.command in {"validate", "plan"}:
            spec = load_experiment(args.spec)
            arms = expand_matrix(spec)
            result: dict[str, Any] = {
                "valid": True, "specDigest": spec_digest(spec), "armCount": len(arms),
            }
            if args.command == "plan":
                result["arms"] = [arm.model_dump(mode="json") for arm in arms]
            emit(result)
            return 0
        if args.command == "run":
            from clawbox.experiments.worker import ExperimentWorker

            spec = load_experiment(args.spec)
            run_id = args.run_id or f"run-{uuid.uuid4().hex[:16]}"
            attempt_id = args.attempt_id or f"attempt-{uuid.uuid4().hex[:16]}"
            owner_id = args.owner_id or attempt_id
            output = args.output_root / run_id
            results = ExperimentWorker(
                spec, run_id=run_id, attempt_id=attempt_id, task_uid=owner_id,
                output_root=output,
            ).run()
            emit({"runId": run_id, "attemptId": attempt_id, "ownerId": owner_id,
                  "output": str(output),
                  "succeeded": all(item.status.value == "succeeded" for item in results)})
            return 0 if all(item.status.value == "succeeded" for item in results) else 1
        run_root = args.output_root / args.run_id
        summary = run_root / "summary.json"
        if not summary.exists():
            raise ValueError(f"run summary does not exist: {summary}")
        value = json.loads(summary.read_text(encoding="utf-8"))
        emit(value if args.command == "collect" else {
            "runId": args.run_id, "output": str(run_root),
            "arms": [{"armId": item.get("arm", {}).get("arm_id"),
                      "status": item.get("status")} for item in value],
        })
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"clawbox: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
