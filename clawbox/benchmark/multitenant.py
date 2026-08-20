"""Concurrent multi-tenant submission and lifecycle load driver.

The driver talks only to the public Managed API. Each simulated tenant gets a
separate ``X-Tenant-Id`` scope, while every run gets a unique idempotency key.
It can measure API intake alone or watch dispatched runs to a terminal phase.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from clawbox.benchmark.managed_client import ManagedAPIClient, input_sha256


TERMINAL_PHASES = {"Succeeded", "Failed", "Cancelled", "TimedOut"}


@dataclass
class TenantSubmission:
    tenant_id: str
    run_ids: list[str] = field(default_factory=list)
    created: int = 0
    replays: int = 0
    phases: dict[str, str] = field(default_factory=dict)

    @property
    def terminal(self) -> int:
        return sum(1 for phase in self.phases.values() if phase in TERMINAL_PHASES)


def submit_tenants(
    *,
    base_url: str,
    token: str,
    tenants: list[str],
    runs_per_tenant: int,
    project_id: str,
    template_ref: str,
    template_revision: int,
    input_ref: str,
    input_sha256_: str,
    deadline_seconds: int,
    idempotency_prefix: str,
    problem_statement: str | None = None,
    submit_workers: int = 1,
    arrival_rate: float = 0.0,
    client_factory: Any = None,
) -> list[TenantSubmission]:
    """Submit one run for every ``(tenant, run number)`` pair."""
    if not tenants:
        raise ValueError("at least one tenant is required")
    if len(set(tenants)) != len(tenants):
        raise ValueError("tenant IDs must be unique")
    if runs_per_tenant < 1 or submit_workers < 1 or arrival_rate < 0:
        raise ValueError("runs/workers must be positive and arrival rate non-negative")

    submissions = {tenant: TenantSubmission(tenant_id=tenant) for tenant in tenants}
    jobs = [
        (tenant, tenant_index, run_index)
        for tenant_index, tenant in enumerate(tenants, start=1)
        for run_index in range(1, runs_per_tenant + 1)
    ]

    def submit(job: tuple[str, int, int]):
        tenant, tenant_index, run_index = job
        client = client_factory(tenant) if client_factory else ManagedAPIClient(
            base_url, token=token, tenant_id=tenant
        )
        try:
            result = client.create_run(
                project_id=project_id,
                template_ref=template_ref,
                template_revision=template_revision,
                input_ref=input_ref,
                input_sha256=input_sha256_,
                deadline_seconds=deadline_seconds,
                idempotency_key=f"{idempotency_prefix}:{tenant_index}:{run_index}",
                problem_statement=problem_statement,
            )
        finally:
            if client_factory is None:
                client.close()
        return tenant, run_index, result

    completed: list[tuple[str, int, Any]] = []
    interval = 1.0 / arrival_rate if arrival_rate else 0.0
    with ThreadPoolExecutor(max_workers=submit_workers) as pool:
        futures = []
        next_submit = time.monotonic()
        for job in jobs:
            if interval:
                delay = next_submit - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                next_submit = max(next_submit + interval, time.monotonic())
            futures.append(pool.submit(submit, job))
        for future in as_completed(futures):
            completed.append(future.result())

    for tenant, run_index, result in sorted(completed, key=lambda item: (item[0], item[1])):
        submission = submissions[tenant]
        submission.run_ids.append(result.run_id)
        submission.created += int(not result.idempotency_replay)
        submission.replays += int(result.idempotency_replay)
        submission.phases[result.run_id] = result.phase
    return [submissions[tenant] for tenant in tenants]


def watch_tenants(
    submissions: list[TenantSubmission],
    *,
    base_url: str,
    token: str,
    client_factory: Any = None,
    poll_seconds: float = 5.0,
    timeout_seconds: float = 600.0,
) -> bool:
    """Poll all runs until terminal; return false when the watch times out."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        total_terminal = 0
        total = 0
        for submission in submissions:
            client = client_factory(submission.tenant_id) if client_factory else ManagedAPIClient(
                base_url, token=token, tenant_id=submission.tenant_id
            )
            try:
                for run_id in submission.run_ids:
                    total += 1
                    try:
                        run = client.get_run(run_id)
                        submission.phases[run_id] = run["phase"]
                    except Exception as exc:
                        print(f"poll error {submission.tenant_id}/{run_id}: {exc}")
                    if submission.phases.get(run_id) in TERMINAL_PHASES:
                        total_terminal += 1
            finally:
                if client_factory is None:
                    client.close()
        print(f"terminal={total_terminal}/{total}")
        if total_terminal >= total:
            return True
        time.sleep(poll_seconds)
    print(f"warning: watch timed out after {timeout_seconds:g}s")
    return False


def render_summary(submissions: list[TenantSubmission]) -> str:
    lines = ["tenant\tcreated\treplays\truns\tphases"]
    for submission in submissions:
        phases = ", ".join(
            f"{run_id}:{phase}" for run_id, phase in submission.phases.items()
        )
        lines.append(
            f"{submission.tenant_id}\t{submission.created}\t{submission.replays}\t"
            f"{len(submission.run_ids)}\t{phases}"
        )
    return "\n".join(lines)


def _tenant_ids(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[str]:
    if args.tenants:
        return args.tenants
    if args.tenant_count < 1:
        parser.error("--tenant-count must be at least 1")
    return [f"{args.tenant_prefix}-{index:03d}" for index in range(1, args.tenant_count + 1)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8085")
    parser.add_argument("--token", default=os.getenv("CLAWBOX_TOKEN"), help="service token; prefer CLAWBOX_TOKEN")
    tenant_group = parser.add_mutually_exclusive_group(required=True)
    tenant_group.add_argument("--tenants", nargs="+", help="explicit tenant IDs")
    tenant_group.add_argument("--tenant-count", type=int, help="generate tenant IDs")
    parser.add_argument("--tenant-prefix", default="load-tenant")
    parser.add_argument("--runs-per-tenant", type=int, default=1)
    parser.add_argument("--submit-workers", type=int, default=16)
    parser.add_argument("--arrival-rate", type=float, default=0.0, help="maximum submissions/second; 0 is unlimited")
    parser.add_argument("--project", default="load-test")
    parser.add_argument("--template", default="swe-rebench-arm64")
    parser.add_argument("--template-revision", type=int, default=1)
    parser.add_argument("--input-ref", default="load-test-input")
    problem = parser.add_mutually_exclusive_group()
    problem.add_argument("--problem", default="Inspect the repository and report its test status.")
    problem.add_argument("--problem-file", type=Path)
    parser.add_argument("--deadline-seconds", type=int, default=300)
    parser.add_argument("--idempotency-prefix", default=None)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--watch-timeout", type=float, default=1800.0)
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--require-success", action="store_true")
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(argv)

    if not args.token:
        parser.error("set CLAWBOX_TOKEN or pass --token")
    tenants = _tenant_ids(args, parser)
    if args.runs_per_tenant < 1 or args.submit_workers < 1 or args.arrival_rate < 0:
        parser.error("runs/workers must be positive and arrival rate non-negative")
    problem_statement = (
        args.problem_file.read_text(encoding="utf-8") if args.problem_file else args.problem
    )
    prefix = args.idempotency_prefix or f"load-{int(time.time())}"
    started = time.monotonic()
    submissions = submit_tenants(
        base_url=args.api_url,
        token=args.token,
        tenants=tenants,
        runs_per_tenant=args.runs_per_tenant,
        project_id=args.project,
        template_ref=args.template,
        template_revision=args.template_revision,
        input_ref=args.input_ref,
        input_sha256_=input_sha256(problem_statement),
        deadline_seconds=args.deadline_seconds,
        idempotency_prefix=prefix,
        problem_statement=problem_statement,
        submit_workers=args.submit_workers,
        arrival_rate=args.arrival_rate,
    )
    submit_elapsed = time.monotonic() - started
    watched_to_terminal = True
    if args.watch:
        watched_to_terminal = watch_tenants(
            submissions,
            base_url=args.api_url,
            token=args.token,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.watch_timeout,
        )
    total = sum(len(item.run_ids) for item in submissions)
    failures = sum(
        phase not in {"Succeeded", "Accepted", "Queued"}
        for item in submissions
        for phase in item.phases.values()
    )
    summary = {
        "tenant_count": len(tenants),
        "run_count": total,
        "submit_elapsed_seconds": round(submit_elapsed, 6),
        "submit_requests_per_second": round(total / submit_elapsed, 3) if submit_elapsed else None,
        "watched_to_terminal": watched_to_terminal,
        "submissions": [asdict(item) for item in submissions],
    }
    print(render_summary(submissions))
    print(json.dumps({key: value for key, value in summary.items() if key != "submissions"}, sort_keys=True))
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.require_success and (not watched_to_terminal or failures):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
