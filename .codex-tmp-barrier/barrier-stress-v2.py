#!/usr/bin/env python3
"""Exercise the v2 barrier with the full 1280-client acceptance fan-in."""

import concurrent.futures
import hashlib
import json
import pathlib
import statistics
import time
import urllib.request


EVIDENCE_ROOT = pathlib.Path("/data/clawbox-acceptance-20261022T102744Z")
descriptor = json.loads((EVIDENCE_ROOT / "current-barrier-v2.json").read_text())
target = int(descriptor["target"])
url = f"http://{descriptor['host']}:{descriptor['port']}"
headers = {"Authorization": descriptor["key"], "Content-Type": "application/json"}


def request(path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def arrive(index: int) -> tuple[float, int]:
    sid = f"barrier-stress-{index:04d}"
    token = hashlib.md5(sid.encode(), usedforsecurity=False).hexdigest()
    started = time.monotonic()
    result = request("/arrive", {"session_id": sid, "token": token})
    return time.monotonic() - started, int(result["arrived"])


started = time.monotonic()
errors: list[str] = []
latencies: list[float] = []
with concurrent.futures.ThreadPoolExecutor(max_workers=target) as pool:
    futures = [pool.submit(arrive, index) for index in range(target)]
    for future in concurrent.futures.as_completed(futures):
        try:
            latency, _ = future.result()
            latencies.append(latency)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

health = request("/health")
release = request("/release", {"stress_test": True}) if not errors else None
receipt = {
    "target": target,
    "successes": len(latencies),
    "errors": errors,
    "health": health,
    "release": release,
    "wall_seconds": time.monotonic() - started,
    "latency_seconds": {
        "min": min(latencies) if latencies else None,
        "median": statistics.median(latencies) if latencies else None,
        "p95": sorted(latencies)[int(0.95 * (len(latencies) - 1))] if latencies else None,
        "max": max(latencies) if latencies else None,
    },
}
destination = pathlib.Path(descriptor["root"]) / "stress-receipt.json"
destination.write_text(json.dumps(receipt, indent=2))
print(json.dumps(receipt, indent=2))
if errors or health != {"arrived": target, "released": False, "target": target}:
    raise SystemExit(1)
