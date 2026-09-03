#!/usr/bin/env python3
"""Verify one logical agent as a Runtime VM + Tool VM CubeSandbox pair."""
from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import time
import uuid

from cubesandbox import Config, NEVER_TIMEOUT, Sandbox, Template


def listed(sandbox_id: str) -> dict | None:
    return next((item for item in Sandbox.list_v2()
                 if str(item.get("sandboxID") or item.get("sandbox_id")) == sandbox_id), None)


def instrumented_tool_check(tool, execution_id: str) -> dict:
    command = (
        "python3 -c \"import hashlib,time; "
        "x=b'x'*4194304; "
        "[hashlib.sha256(x).digest() for _ in range(200)]; "
        "time.sleep(0.25); print('telemetry-ok')\""
    )
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
    assert result.stdout.strip() == "telemetry-ok", result.stdout
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-template", required=True)
    parser.add_argument("--tool-template", required=True)
    parser.add_argument("--node", required=True)
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
        telemetry = instrumented_tool_check(tool, f"pair-{uuid.uuid4().hex}")
        started = time.monotonic()
        tool.pause(wait=True, timeout=90)
        timings["tool_pause_seconds"] = time.monotonic() - started
        assert str((listed(tool.sandbox_id) or {}).get("state", "")).lower() == "paused"
        assert str((listed(runtime.sandbox_id) or {}).get("state", "")).lower() == "running"
        started = time.monotonic()
        tool = Sandbox.connect(tool.sandbox_id)
        timings["tool_restore_seconds"] = time.monotonic() - started
        assert tool.files.read("/workspace/pair-state.txt") == "pair-ok"
        print(json.dumps({"status": "PASS", "owner": owner,
                          "runtime_id": runtime.sandbox_id, "tool_id": tool.sandbox_id,
                          "timings": timings, "telemetry": telemetry}, sort_keys=True))
        return 0
    finally:
        for _role, sandbox in reversed(sandboxes):
            try:
                Sandbox.connect(sandbox.sandbox_id).kill()
            except Exception:
                item = listed(sandbox.sandbox_id)
                if item is not None:
                    Sandbox(item, Config()).kill()


if __name__ == "__main__":
    raise SystemExit(main())
