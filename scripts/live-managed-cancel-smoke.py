"""Live managed lifecycle smoke: create, cancel, and await status projection."""

from __future__ import annotations

import os
import time
import uuid

import httpx


def main() -> None:
    base = os.getenv("CLAWBOX_MANAGED_URL", "http://127.0.0.1:8085")
    headers = {
        "X-Clawbox-Token": os.environ["CLAWBOX_SERVICE_TOKEN"],
        "X-Tenant-Id": "live-smoke",
    }
    key = f"cancel-smoke-{uuid.uuid4()}"
    body = {
        "projectId": "live-validation",
        "templateRef": "swe-rebench-arm64",
        "templateRevision": 1,
        "inputRef": "lifecycle-cancel-smoke",
        "inputSha256": "a" * 64,
        "deadlineSeconds": 300,
        "idempotencyKey": key,
        "problemStatement": "Cancellation lifecycle validation; do not execute a workload.",
    }
    with httpx.Client(timeout=10) as client:
        created = client.post(f"{base}/v1/runs", json=body, headers=headers)
        created.raise_for_status()
        run_id = created.json()["runId"]
        cancelled = client.post(f"{base}/v1/runs/{run_id}/cancel", headers=headers)
        cancelled.raise_for_status()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            run = client.get(f"{base}/v1/runs/{run_id}", headers=headers)
            run.raise_for_status()
            attempts = client.get(f"{base}/v1/runs/{run_id}/attempts", headers=headers)
            attempts.raise_for_status()
            payload = run.json()
            attempt_rows = attempts.json()
            if payload["phase"] == "Cancelled" and attempt_rows:
                assert attempt_rows[-1]["phase"] == "Cancelled"
                print({
                    "runId": run_id,
                    "runPhase": payload["phase"],
                    "attemptPhase": attempt_rows[-1]["phase"],
                    "desiredState": payload["desiredState"],
                })
                return
            time.sleep(2)
    raise RuntimeError("managed cancellation was not projected within 60 seconds")


if __name__ == "__main__":
    main()
