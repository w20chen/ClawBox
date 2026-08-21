"""Live scoped-signature and persistence smoke for the tuning KB."""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta

import httpx

from clawbox.common.config import settings
from clawbox.tuning.schema import CollectionQuality, ToolObservation, utcnow
from clawbox.tuning.validate import sign_observation

TENANT = "live-kb-smoke"
REPO = "clawbox/live-kb-smoke"


def main() -> None:
    headers = {"Authorization": f"Bearer {settings.service_token}"}
    base = "http://127.0.0.1:8086"
    with httpx.Client(timeout=10, headers=headers) as client:
        if "--verify-only" in sys.argv:
            result = client.get(
                f"{base}/v1/kb/generation", params={"tenant_id": TENANT, "repo": REPO}
            )
            result.raise_for_status()
            assert result.json()["generation"] >= 1, result.text
            print({"persistentGeneration": result.json()["generation"]})
            return

        end = utcnow()
        observation = ToolObservation(
            execution_id=f"live-{uuid.uuid4()}",
            tool_name="exec",
            command="python -m pytest -q",
            command_digest="a" * 64,
            repo_fingerprint=REPO,
            run_id="live-kb-smoke",
            start_time=end - timedelta(seconds=2),
            end_time=end,
            duration_sec=2,
            status_code="ok",
            exit_code=0,
            complete=True,
            collection_quality=CollectionQuality.VALID,
            coverage_ratio=1,
            cpu_time_sec=1,
            cpu_utilization_avg_cores=0.5,
            rss_peak_bytes=1024,
        )
        secret = settings.kb_ingest_secret or settings.ingest_secret
        signature = sign_observation(observation, secret, TENANT, REPO)
        body = {
            "tenant_id": TENANT,
            "repo_fingerprint": REPO,
            "observations": [{
                "observation": observation.model_dump(mode="json"),
                "signature": signature,
            }],
        }
        accepted = client.post(f"{base}/v1/kb/observations", json=body)
        accepted.raise_for_status()
        assert accepted.json()["accepted"] == 1, accepted.text

        tampered = observation.model_copy(update={"command": "malicious replacement"})
        body["observations"][0]["observation"] = tampered.model_dump(mode="json")
        rejected = client.post(f"{base}/v1/kb/observations", json=body)
        rejected.raise_for_status()
        assert rejected.json()["accepted"] == 0, rejected.text

        body["tenant_id"] = "cross-tenant-replay"
        body["observations"][0]["observation"] = observation.model_dump(mode="json")
        cross_tenant = client.post(f"{base}/v1/kb/observations", json=body)
        cross_tenant.raise_for_status()
        assert cross_tenant.json()["accepted"] == 0, cross_tenant.text
        print({
            "generation": accepted.json()["generation"],
            "tamperedAccepted": rejected.json()["accepted"],
            "crossTenantAccepted": cross_tenant.json()["accepted"],
        })


if __name__ == "__main__":
    main()
