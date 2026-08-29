#!/usr/bin/env python3
"""Run and verify a paired guest-driven SSH snapshot experiment."""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clawbox.replay.lifecycle import FirecrackerConfig, FirecrackerLifecycle


def _requests(path: Path) -> list[dict]:
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("inference store must contain a JSON array")
    return value


def _wait_pending(path: Path, timeout_s: float) -> list[dict]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        requests = _requests(path)
        if any(not item.get("ready", False) for item in requests):
            return requests
        time.sleep(0.02)
    raise TimeoutError("guest did not submit a pending inference request")


def _wait_complete(store: Path, log: Path, expected: int, timeout_s: float) -> list[dict]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        requests = _requests(store)
        completed = len(requests) == expected and all(item.get("ready", False) for item in requests)
        guest_finished = log.exists() and '\"ok\":true' in log.read_text(
            encoding="utf-8", errors="replace",
        )
        if completed and guest_finished:
            return requests
        time.sleep(0.05)
    raise TimeoutError("guest did not complete all inference and tool actions")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--runtime", required=True, type=Path)
    p.add_argument("--tool", required=True, type=Path)
    p.add_argument("--inference-store", required=True, type=Path)
    p.add_argument("--pending-timeout-s", type=float, default=15.0)
    p.add_argument("--restore-after-s", type=float, default=5.0)
    p.add_argument("--completion-timeout-s", type=float, default=30.0)
    p.add_argument("--expected-requests", type=int, required=True)
    p.add_argument("--output", required=True, type=Path)
    a = p.parse_args()
    if a.expected_requests < 1:
        raise ValueError("expected request count must be positive")
    tool_config = FirecrackerConfig.from_json(a.tool)
    runtime_config = FirecrackerConfig.from_json(a.runtime)
    if runtime_config.log_path is None:
        raise ValueError("runtime Firecracker config requires log_path for completion gating")
    tool = FirecrackerLifecycle(tool_config)
    runtime = FirecrackerLifecycle(runtime_config)
    result = {"ok": False, "expected_requests": a.expected_requests}
    try:
        result["tool_start_s"] = tool.start()
        result["runtime_start_s"] = runtime.start()
        pending = _wait_pending(a.inference_store, a.pending_timeout_s)
        result["pending_request_ids"] = [item["request_id"] for item in pending]
        result["tool_snapshot_s"] = tool.checkpoint_and_evict()
        result["runtime_snapshot_s"] = runtime.checkpoint_and_evict()
        time.sleep(a.restore_after_s)
        result["tool_restore_s"] = tool.restore()
        result["runtime_restore_s"] = runtime.restore()
        completed = _wait_complete(
            a.inference_store, runtime_config.log_path,
            a.expected_requests, a.completion_timeout_s,
        )
        result.update({
            "ok": True,
            "completed_request_ids": [item["request_id"] for item in completed],
            "runtime_completion_marker": True,
        })
    finally:
        try: tool.close()
        finally: runtime.close()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))

if __name__ == "__main__": main()
