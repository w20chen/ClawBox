"""Multi-tenant submission simulator (M1).

Simulate K tenants x N runs through the Managed API and watch each tenant's
Run reach terminal.  Tenant isolation is enforced end-to-end by the API:

* ``X-Tenant-Id`` scopes every resource at the repository layer
  (``clawbox.managed.repo``): a run created by tenant A is a 404 for tenant B.
* the control-plane KB is keyed by ``(tenant_id, repo_fingerprint)``
  (``clawbox.tuning.store``), so observations from tenant A only advance A's
  KB generation.

This module is pure Python and deterministic enough to unit-test the
submit/watch logic against a FastAPI TestClient; the same code runs against
the real M1 smoke stack on the target host (``scripts/m1-multitenant.sh``).

Usage::

    python -m clawbox.benchmark.multitenant \\
        --api-url http://127.0.0.1:8085 \\
        --token clawbox-m1-smoke-token-0001 \\
        --tenants tenant-a tenant-b tenant-c \\
        --runs-per-tenant 2 \\
        --template swe-rebench-arm64 --template-revision 1 \\
        --project mt-sim --input-ref 15five__scim2-filter-parser-13 \\
        --deadline-seconds 300 --watch --watch-timeout 1800
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from typing import Any

from clawbox.benchmark.managed_client import (
    ManagedAPIClient,
    input_sha256,
)


@dataclass
class TenantSubmission:
    tenant_id: str
    run_ids: list[str] = field(default_factory=list)
    created: int = 0
    replays: int = 0
    phases: dict[str, str] = field(default_factory=dict)

    @property
    def terminal(self) -> int:
        return sum(
            1 for phase in self.phases.values() if phase in ("Succeeded", "Failed", "Cancelled")
        )


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
    client_factory: Any = None,
) -> list[TenantSubmission]:
    """Submit ``runs_per_tenant`` runs for each tenant.

    ``client_factory`` (optional) is a callable ``(tenant_id) -> client`` used
    by tests to inject a TestClient-backed ``ManagedAPIClient``; default builds
    an ``httpx``-backed client against ``base_url``.
    """
    submissions: list[TenantSubmission] = []
    for index, tenant_id in enumerate(tenants, start=1):
        client = client_factory(tenant_id) if client_factory else ManagedAPIClient(
            base_url, token=token, tenant_id=tenant_id,
        )
        submission = TenantSubmission(tenant_id=tenant_id)
        for run_index in range(1, runs_per_tenant + 1):
            key = f"{idempotency_prefix}:{index}:{run_index}"
            result = client.create_run(
                project_id=project_id,
                template_ref=template_ref,
                template_revision=template_revision,
                input_ref=input_ref,
                input_sha256=input_sha256_,
                deadline_seconds=deadline_seconds,
                idempotency_key=key,
            )
            submission.run_ids.append(result.run_id)
            if result.idempotency_replay:
                submission.replays += 1
            else:
                submission.created += 1
            submission.phases[result.run_id] = result.phase
        submissions.append(submission)
    return submissions


def watch_tenants(
    submissions: list[TenantSubmission],
    *,
    base_url: str,
    token: str,
    client_factory: Any = None,
    poll_seconds: float = 5.0,
    timeout_seconds: float = 600.0,
) -> None:
    """Poll each tenant's Run phase until terminal or timeout.

    ``client_factory`` (optional) mirrors :func:`submit_tenants`.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        total_terminal = 0
        total = 0
        for submission in submissions:
            client = client_factory(submission.tenant_id) if client_factory else ManagedAPIClient(
                base_url, token=token, tenant_id=submission.tenant_id,
            )
            for run_id in submission.run_ids:
                total += 1
                try:
                    run = client.get_run(run_id)
                    submission.phases[run_id] = run["phase"]
                except Exception as exc:  # transient API/db errors are polled through
                    print(f"  poll error {submission.tenant_id}/{run_id}: {exc}")
                if submission.phases.get(run_id) in ("Succeeded", "Failed", "Cancelled"):
                    total_terminal += 1
        print(f"  terminal={total_terminal}/{total}")
        if total_terminal >= total:
            return
        time.sleep(poll_seconds)
    print(f"  WARN: watch timeout after {timeout_seconds}s")


def render_summary(submissions: list[TenantSubmission]) -> str:
    lines = ["tenant\tcreated\treplays\truns\tphases"]
    for submission in submissions:
        phases = ", ".join(
            f"{rid}:{phase}" for rid, phase in submission.phases.items()
        )
        lines.append(
            f"{submission.tenant_id}\t{submission.created}\t{submission.replays}\t"
            f"{len(submission.run_ids)}\t{phases}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8085")
    parser.add_argument("--token", default="development-only-token")
    parser.add_argument("--tenants", nargs="+", required=True, help="tenant ids")
    parser.add_argument("--runs-per-tenant", type=int, default=1)
    parser.add_argument("--project", default="mt-sim")
    parser.add_argument("--template", default="swe-rebench-arm64")
    parser.add_argument("--template-revision", type=int, default=1)
    parser.add_argument("--input-ref", default="15five__scim2-filter-parser-13")
    parser.add_argument("--problem-statement", default="Fix the scim2 filter parser attribute path handling.")
    parser.add_argument("--deadline-seconds", type=int, default=300)
    parser.add_argument("--idempotency-prefix", default="mt-sim")
    parser.add_argument("--watch", action="store_true", help="poll runs until terminal")
    parser.add_argument("--watch-timeout", type=float, default=1800.0)
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    args = parser.parse_args(argv)

    submissions = submit_tenants(
        base_url=args.api_url,
        token=args.token,
        tenants=args.tenants,
        runs_per_tenant=args.runs_per_tenant,
        project_id=args.project,
        template_ref=args.template,
        template_revision=args.template_revision,
        input_ref=args.input_ref,
        input_sha256_=input_sha256(args.problem_statement),
        deadline_seconds=args.deadline_seconds,
        idempotency_prefix=args.idempotency_prefix,
    )
    if args.watch:
        watch_tenants(
            submissions,
            base_url=args.api_url,
            token=args.token,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.watch_timeout,
        )
    print(render_summary(submissions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
