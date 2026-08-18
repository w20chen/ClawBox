"""Managed API client + benchmark launcher as API client (M1, roadmap §6.5).

Production runs are submitted through the Managed API (idempotency-keyed),
never by writing SandboxTask CRs directly. Direct CR writes remain only in the
dev/benchmark launcher and are the path being retired by ADR-001.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx


class ManagedAPIError(RuntimeError):
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        detail = body.get("detail") if isinstance(body, dict) else None
        super().__init__(f"Managed API {status_code}: {detail or body}")


class RunConflictError(ManagedAPIError):
    """HTTP 409: same idempotency key, different request body."""


class UnknownTemplateError(ManagedAPIError):
    """HTTP 422: unknown template/revision or deadline out of range."""


@dataclass(frozen=True)
class CreateRunResult:
    run_id: str
    phase: str
    idempotency_replay: bool
    current_attempt_id: str | None = None


class ManagedAPIClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        tenant_id: str,
        timeout: float = 30.0,
        client: Any = None,
    ):
        # `client` is any object with .get/.post(headers=..., json=...); an
        # httpx.Client in production, a starlette TestClient in tests.
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)
        self._headers = {"X-Clawbox-Token": token, "X-Tenant-Id": tenant_id}

    def create_run(
        self,
        *,
        project_id: str,
        template_ref: str,
        template_revision: int,
        input_ref: str,
        input_sha256: str,
        deadline_seconds: int,
        idempotency_key: str,
    ) -> CreateRunResult:
        payload = {
            "projectId": project_id,
            "templateRef": template_ref,
            "templateRevision": template_revision,
            "inputRef": input_ref,
            "inputSha256": input_sha256,
            "deadlineSeconds": deadline_seconds,
            "idempotencyKey": idempotency_key,
        }
        response = self._client.post("/v1/runs", headers=self._headers, json=payload)
        if response.status_code in (200, 201):
            data = response.json()
            return CreateRunResult(
                run_id=data["runId"],
                phase=data["phase"],
                idempotency_replay=data["idempotencyReplay"],
                current_attempt_id=data.get("currentAttemptId"),
            )
        if response.status_code == 409:
            raise RunConflictError(response.status_code, response.json())
        if response.status_code == 422:
            raise UnknownTemplateError(response.status_code, response.json())
        raise ManagedAPIError(response.status_code, response.text)

    def get_run(self, run_id: str) -> dict[str, Any]:
        response = self._client.get(f"/v1/runs/{run_id}", headers=self._headers)
        if response.status_code != 200:
            raise ManagedAPIError(response.status_code, response.text)
        return response.json()

    def cancel(self, run_id: str) -> dict[str, Any]:
        response = self._client.post(f"/v1/runs/{run_id}/cancel", headers=self._headers)
        if response.status_code != 200:
            raise ManagedAPIError(response.status_code, response.text)
        return response.json()

    def retry(self, run_id: str) -> dict[str, Any]:
        response = self._client.post(f"/v1/runs/{run_id}/retry", headers=self._headers)
        if response.status_code != 200:
            raise ManagedAPIError(response.status_code, response.text)
        return response.json()

    def list_attempts(self, run_id: str) -> list[dict[str, Any]]:
        response = self._client.get(f"/v1/runs/{run_id}/attempts", headers=self._headers)
        if response.status_code != 200:
            raise ManagedAPIError(response.status_code, response.text)
        return response.json()

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        response = self._client.get(f"/v1/runs/{run_id}/events", headers=self._headers)
        if response.status_code != 200:
            raise ManagedAPIError(response.status_code, response.text)
        return response.json()


def input_sha256(problem_statement: str) -> str:
    return hashlib.sha256(problem_statement.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CLI: submit a SWE-ReBench task list through the Managed API
# ---------------------------------------------------------------------------

def submit_benchmark(
    client: ManagedAPIClient,
    *,
    tasks: list[dict[str, Any]],
    project_id: str,
    template_ref: str,
    template_revision: int,
    deadline_seconds: int,
    idempotency_prefix: str,
) -> list[CreateRunResult]:
    results: list[CreateRunResult] = []
    for task in tasks:
        instance_id = str(task["instance_id"])
        problem = str(task.get("problem_statement") or task.get("problem") or "")
        result = client.create_run(
            project_id=project_id,
            template_ref=template_ref,
            template_revision=template_revision,
            input_ref=instance_id,
            input_sha256=input_sha256(problem),
            deadline_seconds=deadline_seconds,
            idempotency_key=f"{idempotency_prefix}:{instance_id}",
        )
        results.append(result)
    return results


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Submit SWE-ReBench tasks via the Managed API")
    parser.add_argument("--tasks", required=True, help="JSON file of tasks (instance_id/problem_statement/image)")
    parser.add_argument("--api-url", default="http://localhost:8085")
    parser.add_argument("--token", default="development-only-token")
    parser.add_argument("--tenant", default="tenant-a")
    parser.add_argument("--project", default="benchmark")
    parser.add_argument("--template-ref", default="swe-rebench-arm64")
    parser.add_argument("--template-revision", type=int, default=1)
    parser.add_argument("--deadline-seconds", type=int, default=1800)
    parser.add_argument("--idempotency-prefix", default="bench")
    args = parser.parse_args(argv)

    from clawbox.benchmark.kubernetes import load_tasks
    from pathlib import Path

    tasks = [t.__dict__ for t in load_tasks(Path(args.tasks))]
    client = ManagedAPIClient(
        args.api_url, token=args.token, tenant_id=args.tenant,
    )
    results = submit_benchmark(
        client,
        tasks=tasks,
        project_id=args.project,
        template_ref=args.template_ref,
        template_revision=args.template_revision,
        deadline_seconds=args.deadline_seconds,
        idempotency_prefix=args.idempotency_prefix,
    )
    for result in results:
        flag = "replay" if result.idempotency_replay else "created"
        print(f"{result.run_id}\t{result.phase}\t{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
