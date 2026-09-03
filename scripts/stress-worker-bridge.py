#!/usr/bin/env python3
"""Stress the fixed WorkerBridge without provisioning one Tool VM per client."""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clawbox.experiments.openclaw_driver import WorkerBridge
from clawbox.replay.lifecycle import CommandResult


def post(session, command: str, execution_id: str) -> dict:
    body = json.dumps({"command": command, "execution_id": execution_id,
                       "session_id": session.session_id}).encode()
    request = urllib.request.Request(
        session.url + "/execute", data=body, method="POST",
        headers={"Authorization": f"Bearer {session.token}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-host", required=True)
    parser.add_argument("--bridge-port", required=True, type=int)
    args = parser.parse_args()
    started = time.monotonic()
    started_by_id: dict[str, float] = {}
    finished_by_id: dict[str, float] = {}
    routed: dict[str, str] = {}

    def executor(session_id: str):
        def execute(command: str, _timeout: float, execution_id: str) -> CommandResult:
            started_by_id[execution_id] = time.monotonic()
            if command == "long":
                time.sleep(0.5)
            elif command == "medium":
                time.sleep(0.05)
            else:
                time.sleep(0.01)
            routed[execution_id] = session_id
            finished_by_id[execution_id] = time.monotonic()
            return CommandResult(0, session_id, "", finished_by_id[execution_id] - started_by_id[execution_id])
        return execute

    bridge = WorkerBridge(advertise_host=args.bridge_host, advertised_port=args.bridge_port)
    with bridge:
        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(bridge.register, f"stress-session-{i:03d}",
                                   executor(f"stress-session-{i:03d}"),
                                   task_id="bridge-stress", tool_sandbox_id=f"tool-{i:03d}")
                       for i in range(100)]
            sessions = [future.result() for future in futures]
        bridge.wait_ready()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        unauthenticated = urllib.request.Request(bridge.url + "/execute", data=b"{}", method="POST")
        try:
            opener.open(unauthenticated, timeout=10)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("unauthenticated request was accepted")

        results: list[dict] = []
        for level in (1, 8, 20, 40, 60):
            items = [(sessions[i], "short", f"level-{level}-{i}") for i in range(level)]
            with ThreadPoolExecutor(max_workers=level) as pool:
                responses = list(pool.map(lambda item: post(*item), items))
            assert all(response["exit_code"] == 0 for response in responses)
            assert all(response["session_id"] == item[0].session_id
                       and response["execution_id"] == item[2]
                       for response, item in zip(responses, items))
            results.append({"level": level, "requests": len(items)})

        same_session_items = [(sessions[0], "short", "same-session-a"),
                              (sessions[0], "medium", "same-session-b")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            same_session = list(pool.map(lambda item: post(*item), same_session_items))
        assert {item["execution_id"] for item in same_session} == {
            "same-session-a", "same-session-b"}

        long_item = (sessions[0], "long", "hol-long")
        with ThreadPoolExecutor(max_workers=12) as pool:
            long_future = pool.submit(post, *long_item)
            time.sleep(0.03)
            short_items = [(sessions[i], "short", f"hol-short-{i}") for i in range(1, 9)]
            short_futures = [pool.submit(post, *item) for item in short_items]
            short_responses = [future.result() for future in short_futures]
            long_response = long_future.result()
        assert long_response["execution_id"] == "hol-long"
        assert max(finished_by_id[item[2]] for item in short_items) < finished_by_id["hol-long"]
        assert all(response["session_id"] == item[0].session_id
                   for response, item in zip(short_responses, short_items))

        bad_session = bridge.register("failure-session", lambda *_: (_ for _ in ()).throw(
            RuntimeError("intentional executor failure")), task_id="bridge-stress")
        failed_body = json.dumps({"command": "bad", "execution_id": "failure-1",
                                  "session_id": bad_session.session_id}).encode()
        failed_request = urllib.request.Request(
            bad_session.url + "/execute", data=failed_body, method="POST",
            headers={"Authorization": f"Bearer {bad_session.token}",
                     "Content-Type": "application/json"},
        )
        try:
            opener.open(failed_request, timeout=10)
        except urllib.error.HTTPError as exc:
            assert exc.code == 500
        else:
            raise AssertionError("failed executor did not return 500")
        assert post(sessions[1], "short", "after-failure")["session_id"] == "stress-session-001"
        assert bad_session.close(timeout=5)
        for session in sessions:
            assert session.close(timeout=5)
        assert bridge.session_count == 0

    record = {
        "status": "PASS", "elapsed_seconds": time.monotonic() - started,
        "levels": results, "registered_sessions": 101,
        "completed_requests": len(routed), "hol_short_finished_before_long": True,
        "registry_after_cleanup": bridge.session_count,
        "request_records": len(bridge.requests),
        "secrets_logged": False,
    }
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
