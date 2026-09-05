#!/usr/bin/env python3
"""Measure metadata-only PolicyControl overhead without Tool or Cube latency."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from clawbox.experiments.policy_control import PolicyControlServer


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def post(session, path: str, execution_id: str) -> None:
    payload = json.dumps({
        "session_id": session.session_id,
        "execution_id": execution_id,
        "operation": "exec",
        "command_sha256": hashlib.sha256(execution_id.encode()).hexdigest(),
    }).encode()
    request = urllib.request.Request(
        session.url + path, data=payload, method="POST",
        headers={"Authorization": f"Bearer {session.token}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=40)
    parser.add_argument("--requests-per-session", type=int, default=25)
    parser.add_argument("--max-p95-ms", type=float, default=None)
    args = parser.parse_args()
    if args.concurrency < 1 or args.requests_per_session < 1:
        parser.error("concurrency and requests-per-session must be positive")

    server = PolicyControlServer(
        advertise_host="127.0.0.1", advertised_port=0,
        bind_host="127.0.0.1", bind_port=0,
    )
    server.advertised_port = server.actual_port
    latencies: list[float] = []
    with server:
        sessions = [
            server.register(
                f"bench-{index:03d}", admit=lambda _request: {"decision": "ADMIT"},
                complete=lambda _request: {"status": "COMPLETED"},
            )
            for index in range(args.concurrency)
        ]

        def run_session(index: int) -> list[float]:
            observed = []
            session = sessions[index]
            for request_index in range(args.requests_per_session):
                execution_id = f"exec-{index:03d}-{request_index:05d}"
                started = time.perf_counter()
                post(session, "/v1/tool/admit", execution_id)
                post(session, "/v1/tool/complete", execution_id)
                observed.append(time.perf_counter() - started)
            return observed

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for observed in pool.map(run_session, range(args.concurrency)):
                latencies.extend(observed)
        for session in sessions:
            if not session.close(timeout=5):
                raise RuntimeError(f"benchmark session did not drain: {session.session_id}")

    result = {
        "status": "PASS",
        "concurrency": args.concurrency,
        "requests": len(latencies),
        "measurement": "admit_plus_complete_loopback_round_trip",
        "mean_ms": statistics.fmean(latencies) * 1000,
        "p50_ms": percentile(latencies, 0.50) * 1000,
        "p95_ms": percentile(latencies, 0.95) * 1000,
        "max_ms": max(latencies) * 1000,
    }
    if args.max_p95_ms is not None and result["p95_ms"] > args.max_p95_ms:
        result["status"] = "FAIL"
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
