#!/usr/bin/env python3
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx


def inventory(client: httpx.Client, api_url: str) -> list[dict]:
    response = client.get(f"{api_url}/sandboxes")
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("snapshot", "cleanup"))
    parser.add_argument("--api-url", default="http://127.0.0.1:3000")
    parser.add_argument("--api-key", default="e2b_000000")
    parser.add_argument("--template-id", required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    headers = {"X-API-Key": args.api_key}
    with httpx.Client(headers=headers, timeout=60, trust_env=False) as client:
        before = inventory(client, args.api_url)
        if args.action == "snapshot":
            args.baseline.write_text(json.dumps(before, indent=2, sort_keys=True) + "\n")
            args.output.write_text(json.dumps({"inventory_count": len(before)}, indent=2) + "\n")
            return

        baseline_ids = {row["sandboxID"] for row in json.loads(args.baseline.read_text())}
        candidates = [
            row for row in before
            if row.get("sandboxID") not in baseline_ids
            and row.get("templateID") == args.template_id
        ]

        def delete(sandbox_id: str) -> dict:
            with httpx.Client(headers=headers, timeout=60, trust_env=False) as worker:
                response = worker.delete(f"{args.api_url}/sandboxes/{sandbox_id}")
                return {"sandbox_id": sandbox_id, "status_code": response.status_code,
                        "body": response.text[:500]}

        results = []
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(delete, row["sandboxID"]) for row in candidates]
            for future in as_completed(futures):
                results.append(future.result())

        deadline = time.monotonic() + 120
        remaining = []
        while time.monotonic() < deadline:
            after = inventory(client, args.api_url)
            remaining = [
                row for row in after
                if row.get("sandboxID") not in baseline_ids
                and row.get("templateID") == args.template_id
            ]
            health = client.get(f"{args.api_url}/health").json()
            if not remaining and health.get("sandboxes") == 0:
                break
            time.sleep(1)
        report = {
            "baseline_inventory_count": len(baseline_ids),
            "matched_new_sandboxes": len(candidates),
            "delete_status_counts": {},
            "remaining_matching_sandboxes": remaining,
            "health": health,
        }
        for result in results:
            key = str(result["status_code"])
            report["delete_status_counts"][key] = report["delete_status_counts"].get(key, 0) + 1
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        if remaining or health.get("sandboxes") != 0 or any(
            result["status_code"] not in (200, 204) for result in results
        ):
            raise SystemExit(2)


if __name__ == "__main__":
    main()
