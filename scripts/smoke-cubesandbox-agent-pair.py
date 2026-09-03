#!/usr/bin/env python3
"""Verify one logical agent as a Runtime VM + Tool VM CubeSandbox pair."""
from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

from cubesandbox import NEVER_TIMEOUT, Sandbox, Template

from clawbox.cube.api_retry import read_with_backoff
from clawbox.experiments.openclaw_driver import WorkerBridge
from clawbox.replay.lifecycle import CommandResult


def listed(sandbox_id: str) -> dict | None:
    return next((item for item in read_with_backoff(
                     Sandbox.list_v2, label="Sandbox.list_v2")
                 if str(item.get("sandboxID") or item.get("sandbox_id")) == sandbox_id), None)


def instrumented_tool_check(tool, execution_id: str) -> dict:
    # Keep the acceptance command deterministic and short. The collector's
    # validity, cgroup sampling, and loss gates—not synthetic CPU load—are the
    # subject of this smoke.
    suffix = "before" if "before" in execution_id else "after"
    command = f"printf telemetry-ok-{suffix}"
    envelope = "__CBX_EXEC_1__" + json.dumps(
        {"v": 1, "execution_id": execution_id}, separators=(",", ":"),
    ) + "\n" + command
    encoded = base64.b64encode(envelope.encode()).decode()
    result = tool.commands.run(
        "TOOL_EXEC_TIMEOUT_SECONDS=30 /usr/local/bin/tool-bridge "
        f"--execute-base64 {shlex.quote(encoded)}",
        timeout=45, cwd="/workspace",
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == f"telemetry-ok-{suffix}", result.stdout
    marker = "CLAWBOX_TELEMETRY_RECORD="
    records = [
        json.loads(line.removeprefix(marker))
        for line in result.stderr.splitlines() if line.startswith(marker)
    ]
    assert len(records) == 1, result.stderr
    record = records[0]
    assert record.get("execution_id") == execution_id, record
    assert record.get("telemetry_state") == "complete", record
    assert record.get("telemetry_collection_validity") == "valid", record
    artifact_path = str(record.get("telemetry_artifact") or "")
    artifact_root = "/var/lib/clawtune/artifacts/tool-resource/"
    assert artifact_path.startswith(artifact_root), record

    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", execution_id)
    cgroup = json.loads(tool.files.read(
        f"{artifact_root}cgroup-resource-{safe_id}.json",
    ))
    native = json.loads(tool.files.read(artifact_path))
    assert cgroup.get("execution_id") == execution_id, cgroup
    assert cgroup.get("source") == "cgroup-v2", cgroup
    assert cgroup.get("sampling_quality") == "valid", cgroup
    assert native.get("collection_validity") == "valid", native
    assert native.get("cleanup") == "ok", native
    calls = native.get("calls") or []
    assert len(calls) == 1 and calls[0].get("tool_call_id") == execution_id, native
    return {
        "execution_id": execution_id,
        "cgroup_source": cgroup["source"],
        "cgroup_sampling_quality": cgroup["sampling_quality"],
        "ebpf_collection_validity": native["collection_validity"],
        "ebpf_cleanup": native["cleanup"],
        "ebpf_loss_total": int((native.get("telemetry_loss_total") or {}).get("total") or 0),
    }


def assert_running(sandbox_id: str, label: str) -> None:
    state = str((listed(sandbox_id) or {}).get("state", "")).lower()
    assert state == "running", f"{label} is not running: {state!r}"


def runtime_worker_tool_check(runtime, tool, *, bridge_host: str, bridge_port: int) -> dict:
    """Exercise Runtime -> NodePort -> fixed WorkerBridge -> Tool concurrently.

    This is deliberately a minimal bridge check, not the full exact-ID
    validation performed by the worker's persisted trace pipeline.
    """
    captured: dict[str, dict] = {}

    def execute(session_id: str, command: str, timeout: float, received_id: str) -> CommandResult:
        envelope = "__CBX_EXEC_1__" + json.dumps(
            {"v": 1, "execution_id": received_id}, separators=(",", ":"),
        ) + "\n" + command
        encoded = base64.b64encode(envelope.encode()).decode()
        result = tool.commands.run(
            "TOOL_EXEC_TIMEOUT_SECONDS=30 /usr/local/bin/tool-bridge "
            f"--execute-base64 {shlex.quote(encoded)}", timeout=timeout, cwd="/workspace",
        )
        marker = "CLAWBOX_TELEMETRY_RECORD="
        records = [json.loads(line.removeprefix(marker)) for line in result.stderr.splitlines()
                   if line.startswith(marker)]
        assert len(records) == 1, result.stderr
        captured[received_id] = {"record": records[0], "session_id": session_id,
                                 "tool_sandbox_id": tool.sandbox_id}
        return CommandResult(result.exit_code, result.stdout, "", result.duration_s)

    bridge = WorkerBridge(advertise_host=bridge_host, advertised_port=bridge_port)
    with bridge:
        session_a = bridge.register(
            "bridge-session-a",
            lambda command, timeout, execution_id: execute(
                "bridge-session-a", command, timeout, execution_id),
            task_id="pair-smoke", tool_sandbox_id=tool.sandbox_id,
        )
        session_b = bridge.register(
            "bridge-session-b",
            lambda command, timeout, execution_id: execute(
                "bridge-session-b", command, timeout, execution_id),
            task_id="pair-smoke", tool_sandbox_id=tool.sandbox_id,
        )
        bridge.wait_ready()
        probe = urllib.request.Request(
            "http://127.0.0.1:18080/execute", data=b"{}", method="POST",
        )
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(probe)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc
        else:
            raise AssertionError("unauthenticated bridge probe was not rejected with 401")

        wrong_token = runtime.commands.run(
            f"curl --noproxy '*' -sS -o /dev/null -w '%{{http_code}}' -X POST "
            f"-H 'Authorization: Bearer not-a-token' {shlex.quote(bridge.url + '/execute')}",
            timeout=30, cwd="/workspace",
        )
        assert wrong_token.exit_code == 0 and wrong_token.stdout.strip() == "401", wrong_token
        cross_payload = base64.b64encode(json.dumps({
            "command": "printf should-not-run", "execution_id": "bridge-cross",
            "session_id": session_b.session_id,
        }, separators=(",", ":")).encode()).decode()
        cross = runtime.commands.run(
            f"body=$(echo {shlex.quote(cross_payload)} | base64 -d); "
            f"curl --noproxy '*' -sS -o /dev/null -w '%{{http_code}}' -X POST "
            f"-H 'Authorization: Bearer {session_a.token}' -H 'Content-Type: application/json' "
            f"--data \"$body\" {shlex.quote(bridge.url + '/execute')}",
            timeout=30, cwd="/workspace",
        )
        assert cross.exit_code == 0 and cross.stdout.strip() == "403", cross

        calls = [
            (session_a, "bridge-a-" + uuid.uuid4().hex, "runtime-worker-tool-a"),
            (session_b, "bridge-b-" + uuid.uuid4().hex, "runtime-worker-tool-b"),
        ]

        def runtime_call(item):
            session, execution_id, output = item
            payload = base64.b64encode(json.dumps({
                "command": f"printf {output}", "execution_id": execution_id,
                "session_id": session.session_id, "timeout_seconds": 30,
            }, separators=(",", ":")).encode()).decode()
            command = (
                f"body=$(echo {shlex.quote(payload)} | base64 -d); "
                f"curl --noproxy '*' -fsS -X POST -H 'Authorization: Bearer {session.token}' "
                "-H 'Content-Type: application/json' "
                f"--data \"$body\" {shlex.quote(session.url + '/execute')}"
            )
            return runtime.commands.run(command, timeout=45, cwd="/workspace")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(runtime_call, calls))
        assert all(result.exit_code == 0 for result in results), results
        responses = [json.loads(result.stdout) for result in results]
        for response, (session, execution_id, output) in zip(responses, calls):
            assert response.get("stdout") == output, response
            assert response.get("execution_id") == execution_id, response
            assert response.get("session_id") == session.session_id, response
            observed = captured[execution_id]
            record = observed["record"]
            assert observed["session_id"] == session.session_id
            assert observed["tool_sandbox_id"] == tool.sandbox_id
            assert record.get("execution_id") == execution_id
            assert record.get("telemetry_state") == "complete"
            artifact_root = "/var/lib/clawtune/artifacts/tool-resource/"
            safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", execution_id)
            cgroup = json.loads(tool.files.read(f"{artifact_root}cgroup-resource-{safe_id}.json"))
            native = json.loads(tool.files.read(str(record["telemetry_artifact"])))
            assert cgroup.get("execution_id") == execution_id
            assert cgroup.get("source") == "cgroup-v2"
            assert cgroup.get("sampling_quality") == "valid"
            assert native.get("execution_id") == execution_id
            assert native.get("collection_validity") == "valid"
            assert native.get("cleanup") == "ok"
            assert int((native.get("telemetry_loss_total") or {}).get("total") or 0) == 0
        assert session_a.close()
        assert session_b.close()
    assert bridge.session_count == 0
    return {"execution_ids": [item[1] for item in calls], "exact_id_join": 1.0,
            "endpoint": bridge.url, "startup": bridge.startup,
            "worker_bridge_execution_ids": [item[1] for item in calls],
            "tool_record_execution_ids": [item[1] for item in calls]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-template", required=True)
    parser.add_argument("--tool-template", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--bridge-host", required=True,
                        help="worker host address reachable from the Runtime VM")
    parser.add_argument("--bridge-port", required=True, type=int,
                        help="allocated NodePort for this Worker")
    args = parser.parse_args()
    owner = f"pair-smoke-{uuid.uuid4().hex}"
    sandboxes = []
    timings = {}
    try:
        for template_id in (args.runtime_template, args.tool_template):
            assert str(Template.get(template_id).status).lower() in {"ready", "succeeded"}
        for role, template_id in (("runtime", args.runtime_template), ("tool", args.tool_template)):
            started = time.monotonic()
            sandbox = Sandbox.create(
                template=template_id, timeout=NEVER_TIMEOUT,
                lifecycle={"on_timeout": "kill", "auto_resume": False},
                metadata={"clawbox.owner": owner, "clawbox.role": role},
                distribution_scope=[args.node],
                env_vars=({
                    "CLAWBOX_VM_ROLE": "tool",
                    "TOOL_BRIDGE_LOG_PATH": "/var/lib/clawtune/artifacts/tool-bridge.jsonl",
                    "TOOL_BRIDGE_WORKDIR": "/workspace",
                    "TASK_ID": owner,
                    "CELL_ID": "pair-smoke",
                    "CLAWBOX_REPOSITORY": "clawbox/pair-smoke",
                } if role == "tool" else {"CLAWBOX_VM_ROLE": "runtime"}),
            )
            timings[f"{role}_create_seconds"] = time.monotonic() - started
            sandboxes.append((role, sandbox))
        runtime = sandboxes[0][1]
        tool = sandboxes[1][1]
        runtime_check = runtime.commands.run(
            "openclaw --version && "
            "test -f /opt/clawtune/packages/clawtune-plugin/dist/index.js && "
            "/opt/clawtune/venv/bin/python -c 'import clawtune_sidecar,tool_resource' && "
            "test ! -e /run/clawtune/guest-collector.sock",
            timeout=30, cwd="/workspace",
        )
        assert runtime_check.exit_code == 0, runtime_check.stderr
        tool_check = tool.commands.run(
            "test -x /usr/local/bin/tool-bridge && "
            "test -f /opt/clawtune-guest/tools/guest_collector_server.py && "
            "test -f /sys/fs/cgroup/cgroup.controllers && printf pair-ok > pair-state.txt",
            timeout=30, cwd="/workspace",
        )
        assert tool_check.exit_code == 0, tool_check.stderr
        before = instrumented_tool_check(tool, f"pair-before-{uuid.uuid4().hex}")
        assert before["ebpf_loss_total"] == 0, before
        started = time.monotonic()
        tool.pause(wait=True, timeout=90)
        timings["tool_pause_seconds"] = time.monotonic() - started
        assert str((listed(tool.sandbox_id) or {}).get("state", "")).lower() == "paused"
        assert_running(runtime.sandbox_id, "Runtime while Tool paused")
        started = time.monotonic()
        tool = Sandbox.connect(tool.sandbox_id)
        timings["tool_restore_seconds"] = time.monotonic() - started
        assert_running(tool.sandbox_id, "Tool after restore")
        after = instrumented_tool_check(tool, f"pair-after-{uuid.uuid4().hex}")
        assert after["ebpf_loss_total"] == 0, after
        assert_running(runtime.sandbox_id, "Runtime after Tool restore")
        assert tool.files.read("/workspace/pair-state.txt") == "pair-ok"
        bridge = runtime_worker_tool_check(runtime, tool, bridge_host=args.bridge_host,
                                            bridge_port=args.bridge_port)
        print(json.dumps({"status": "PASS", "owner": owner,
                          "runtime_id": runtime.sandbox_id, "tool_id": tool.sandbox_id,
                          "timings": timings, "telemetry": {"before": before, "after": after},
                          "exact_id_validation": False, "runtime_worker_tool": bridge}, sort_keys=True))
        return 0
    finally:
        cleanup_error = None
        for _role, sandbox in reversed(sandboxes):
            try:
                read_with_backoff(lambda: sandbox.kill(),
                                  label=f"kill {sandbox.sandbox_id}", attempts=4)
            except Exception as exc:
                # A paused handle can be killed without connect/resume. If the
                # API is unavailable, retain the ID and let the owner audit
                # report it instead of silently claiming cleanup.
                cleanup_error = cleanup_error or exc
        try:
            remaining = read_with_backoff(Sandbox.list_v2, label="final owner audit")
            leaked = [item for item in remaining
                      if (item.get("metadata") or {}).get("clawbox.owner") == owner]
            if leaked:
                raise RuntimeError(f"pair smoke cleanup incomplete for owner {owner}: {leaked}")
        except Exception as exc:
            # Preserve the cleanup failure as a visible process failure while
            # never masking the original exception from the test body.
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None and sys.exc_info()[0] is None:
            raise cleanup_error


if __name__ == "__main__":
    raise SystemExit(main())
