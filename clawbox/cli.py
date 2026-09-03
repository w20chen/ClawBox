"""ClawBox experiment command-line client."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

from clawbox.benchmark.managed_client import ManagedAPIClient, ManagedAPIError
from clawbox.experiments import expand_matrix, load_experiment, spec_digest


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--api-url", default=os.getenv("CLAWBOX_API_URL", "http://127.0.0.1:8085"))
    root.add_argument("--token", default=os.getenv("CLAWBOX_TOKEN"))
    root.add_argument("--tenant", default=os.getenv("CLAWBOX_TENANT", "default"))
    commands = root.add_subparsers(dest="group", required=True)
    experiment = commands.add_parser("experiment")
    sub = experiment.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan", "run"):
        command = sub.add_parser(name)
        command.add_argument("spec", type=Path)
        if name == "run":
            command.add_argument("--project", default="default")
            command.add_argument("--idempotency-key")
    for name in ("status", "cancel", "collect"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
    return root


def api_client(args: argparse.Namespace, root: argparse.ArgumentParser) -> ManagedAPIClient:
    if not args.token:
        root.error("set CLAWBOX_TOKEN or pass --token")
    return ManagedAPIClient(args.api_url, token=args.token, tenant_id=args.tenant)


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    try:
        if args.command in {"validate", "plan"}:
            spec = load_experiment(args.spec)
            arms = expand_matrix(spec)
            result: dict[str, Any] = {"valid": True, "specDigest": spec_digest(spec),
                                      "armCount": len(arms)}
            if args.command == "plan":
                result["arms"] = [arm.model_dump(mode="json") for arm in arms]
            emit(result)
            return 0
        with api_client(args, root) as client:
            if args.command == "run":
                spec = load_experiment(args.spec)
                created = client.create_experiment(
                    project_id=args.project, experiment_spec=spec.model_dump(mode="json"),
                    idempotency_key=args.idempotency_key or f"experiment-{uuid.uuid4()}",
                )
                value = {"runId": created.run_id, "phase": created.phase,
                         "idempotencyReplay": created.idempotency_replay}
            elif args.command == "status":
                value = client.get_run(args.run_id)
            elif args.command == "cancel":
                value = client.cancel(args.run_id)
            else:
                attempts = client.list_attempts(args.run_id)
                reference = attempts[-1].get("resultManifestRef") if attempts else None
                value = {"runId": args.run_id, "resultReference": reference}
                if reference and Path(reference, "summary.json").exists():
                    value["summary"] = json.loads(Path(reference, "summary.json").read_text())
            emit(value)
            return 0
    except (ManagedAPIError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"clawbox: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
