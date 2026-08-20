"""Command-line client for the ClawBox Managed API."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from clawbox.benchmark.managed_client import ManagedAPIClient, input_sha256


TERMINAL_PHASES = {"Succeeded", "Failed", "Cancelled", "TimedOut"}


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _problem(args: argparse.Namespace) -> str:
    if args.problem_file:
        return args.problem_file.read_text(encoding="utf-8")
    return args.problem or args.input_ref


def _client(args: argparse.Namespace, parser: argparse.ArgumentParser) -> ManagedAPIClient:
    token = args.token or os.getenv("CLAWBOX_TOKEN")
    if not token:
        parser.error("set CLAWBOX_TOKEN or pass --token")
    return ManagedAPIClient(args.api_url, token=token, tenant_id=args.tenant)


def _watch(client: ManagedAPIClient, run_id: str, timeout: float, poll: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.get_run(run_id)
        print(f"{run_id}\t{last['phase']}")
        if last["phase"] in TERMINAL_PHASES:
            return last
        time.sleep(poll)
    raise TimeoutError(f"run {run_id} did not become terminal within {timeout:g}s")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.getenv("CLAWBOX_API_URL", "http://127.0.0.1:8085"))
    parser.add_argument("--token", help="service token; prefer CLAWBOX_TOKEN")
    parser.add_argument("--tenant", default=os.getenv("CLAWBOX_TENANT", "default"))
    commands = parser.add_subparsers(dest="command", required=True)

    submit = commands.add_parser("submit", help="create an idempotent run")
    submit.add_argument("--project", default="default")
    submit.add_argument("--template", default="swe-rebench-arm64")
    submit.add_argument("--template-revision", type=int, default=1)
    submit.add_argument("--input-ref", required=True)
    problem = submit.add_mutually_exclusive_group()
    problem.add_argument("--problem")
    problem.add_argument("--problem-file", type=Path)
    submit.add_argument("--deadline-seconds", type=int, default=1800)
    submit.add_argument("--idempotency-key", required=True)
    submit.add_argument("--watch", action="store_true")
    submit.add_argument("--watch-timeout", type=float, default=2700)
    submit.add_argument("--poll-seconds", type=float, default=10)

    for name in ("status", "cancel", "retry", "attempts", "events"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
    watch = commands.add_parser("watch")
    watch.add_argument("run_id")
    watch.add_argument("--timeout", type=float, default=2700)
    watch.add_argument("--poll-seconds", type=float, default=10)
    return parser


def _run(args: argparse.Namespace, client: ManagedAPIClient) -> int:
    if args.command == "submit":
        problem = _problem(args)
        result = client.create_run(
            project_id=args.project,
            template_ref=args.template,
            template_revision=args.template_revision,
            input_ref=args.input_ref,
            input_sha256=input_sha256(problem),
            deadline_seconds=args.deadline_seconds,
            idempotency_key=args.idempotency_key,
            problem_statement=problem,
        )
        value = {
            "runId": result.run_id,
            "phase": result.phase,
            "idempotencyReplay": result.idempotency_replay,
            "currentAttemptId": result.current_attempt_id,
        }
        _print(value)
        if args.watch:
            _print(_watch(client, result.run_id, args.watch_timeout, args.poll_seconds))
        return 0
    if args.command == "status":
        value = client.get_run(args.run_id)
    elif args.command == "cancel":
        value = client.cancel(args.run_id)
    elif args.command == "retry":
        value = client.retry(args.run_id)
    elif args.command == "attempts":
        value = client.list_attempts(args.run_id)
    elif args.command == "events":
        value = client.list_events(args.run_id)
    else:
        value = _watch(client, args.run_id, args.timeout, args.poll_seconds)
    _print(value)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = _client(args, parser)
    try:
        return _run(args, client)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()


if __name__ == "__main__":
    raise SystemExit(main())
