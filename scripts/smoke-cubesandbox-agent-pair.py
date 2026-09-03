#!/usr/bin/env python3
"""Verify one logical agent as a Runtime VM + Tool VM CubeSandbox pair."""
from __future__ import annotations

import argparse
import json
import time
import uuid

from cubesandbox import Config, NEVER_TIMEOUT, Sandbox, Template


def listed(sandbox_id: str) -> dict | None:
    return next((item for item in Sandbox.list_v2()
                 if str(item.get("sandboxID") or item.get("sandbox_id")) == sandbox_id), None)


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
            )
            timings[f"{role}_create_seconds"] = time.monotonic() - started
            sandboxes.append((role, sandbox))
        runtime = sandboxes[0][1]
        tool = sandboxes[1][1]
        runtime_check = runtime.commands.run(
            "openclaw --version && test -f /opt/clawtune/packages/clawtune-plugin/dist/index.js",
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
                          "timings": timings}, sort_keys=True))
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
