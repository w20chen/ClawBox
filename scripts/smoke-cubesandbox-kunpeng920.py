#!/usr/bin/env python3
"""Functional create/command/file/pause/resume/kill smoke test."""

from __future__ import annotations

import argparse
import json
import time
import uuid

from cubesandbox import Config, NEVER_TIMEOUT, Sandbox, Template


def _state(item: dict) -> str:
    return str(item.get("state", "")).lower()


def _id(item: dict) -> str:
    return str(item.get("sandboxID") or item.get("sandbox_id") or "")


def owned(owner: str) -> list[dict]:
    return [item for item in Sandbox.list_v2() if (item.get("metadata") or {}).get("clawbox.owner") == owner]


def kill_without_resume(item: dict) -> None:
    """Kill a listed sandbox directly; connect() would resume a paused VM."""
    Sandbox(item, Config()).kill()


def mem_available_mib() -> int:
    for line in open("/proc/meminfo", encoding="ascii"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise RuntimeError("MemAvailable is absent from /proc/meminfo")


def wait_state(sandbox_id: str, expected: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = [item for item in Sandbox.list_v2() if _id(item) == sandbox_id]
        if matches and _state(matches[0]) == expected:
            return
        time.sleep(1)
    raise TimeoutError(f"sandbox {sandbox_id} did not reach {expected}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True)
    parser.add_argument("--node", required=True)
    args = parser.parse_args()
    owner = f"clawbox-smoke-{uuid.uuid4().hex}"
    sandbox = None
    memory = {"before_create_mib": mem_available_mib()}
    try:
        health = Sandbox.health()
        if str(health.get("status", "")).lower() not in {"ok", "healthy"}:
            raise RuntimeError(f"CubeAPI is unhealthy: {health}")
        template = Template.get(args.template)
        if str(template.status).lower() not in {"ready", "succeeded", "success", "completed"}:
            raise RuntimeError(f"template {args.template} is not READY: {template.status}")
        sandbox = Sandbox.create(
            template=args.template,
            timeout=NEVER_TIMEOUT,
            lifecycle={"on_timeout": "kill", "auto_resume": False},
            metadata={"clawbox.owner": owner},
            distribution_scope=[args.node],
        )
        result = sandbox.commands.run("printf clawbox-cube-ok", timeout=30, cwd="/workspace")
        assert result.exit_code == 0 and result.stdout == "clawbox-cube-ok"
        sandbox.files.write("/workspace/clawbox-smoke.txt", "state-survives-pause\n")
        process = sandbox.commands.run(
            "python -c 'import time; x=bytearray(512*1024*1024); "
            "x[::4096]=b\"x\"*(512*256); time.sleep(300)' "
            ">/dev/null 2>&1 & echo $! > /workspace/clawbox-smoke.pid",
            timeout=30, cwd="/workspace",
        )
        assert process.exit_code == 0
        time.sleep(3)
        memory["resident_mib"] = mem_available_mib()
        sandbox.pause(wait=True, timeout=90)
        wait_state(sandbox.sandbox_id, "paused")
        time.sleep(2)
        memory["paused_mib"] = mem_available_mib()
        memory["pause_reclaimed_mib"] = memory["paused_mib"] - memory["resident_mib"]
        sandbox = Sandbox.connect(sandbox.sandbox_id)
        time.sleep(2)
        memory["restored_mib"] = mem_available_mib()
        assert sandbox.files.read("/workspace/clawbox-smoke.txt") == "state-survives-pause\n"
        process = sandbox.commands.run(
            "kill -0 $(cat /workspace/clawbox-smoke.pid)", timeout=30, cwd="/workspace",
        )
        assert process.exit_code == 0
        print(f"PASS sandbox={sandbox.sandbox_id} owner={owner}")
        print(json.dumps({"host_memory": memory}, sort_keys=True))
        return 0
    finally:
        if sandbox is not None:
            sandbox.kill()
        for leaked in owned(owner):
            kill_without_resume(leaked)
        if owned(owner):
            raise RuntimeError(f"smoke cleanup failed for owner {owner}")


if __name__ == "__main__":
    raise SystemExit(main())
