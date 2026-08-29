#!/usr/bin/env python3
"""Create per-session credentials and guest config for the SSH replay path."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from clawbox.replay.trace import load_trace


def run(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--inference-url", required=True)
    parser.add_argument("--tool-address", required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.mkdir(parents=True)
    runtime_key = args.output / "runtime_ed25519"
    tool_key = args.output / "tool_host_ed25519"
    run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(runtime_key))
    run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tool_key))
    host_public = tool_key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    (args.output / "known_hosts").write_text(
        f"[{args.tool_address.rsplit(':', 1)[0]}]:{args.tool_address.rsplit(':', 1)[1]} {host_public}\n",
        encoding="utf-8",
    )
    actions = []
    for item in load_trace(args.trace):
        action = {"kind": item.kind, "action_id": item.action_id}
        if item.kind == "llm":
            action.update({"content": json.dumps(item.input, ensure_ascii=False),
                           "recorded_latency_ms": round(item.duration_s * 1000)})
        else:
            action.update({"command": item.shell_command(),
                           "expected_exit_code": item.expected_exit_code or 0})
        actions.append(action)
    (args.output / "replay.json").write_text(json.dumps({
        "session_id": args.session_id, "inference_url": args.inference_url,
        "state_path": "/var/lib/clawbox/replay-state.json",
        "ssh": {"address": args.tool_address, "user": "executor",
                "identity_file": "/etc/clawbox/ssh/runtime_ed25519",
                "known_hosts_file": "/etc/clawbox/ssh/known_hosts"},
        "actions": actions,
    }, indent=2) + "\n", encoding="utf-8")
    (args.output / "tool-init.sh").write_text("""#!/bin/sh
export TOOL_BRIDGE_LISTEN=0.0.0.0:2222
export TOOL_BRIDGE_WORKDIR=/workspace
export TOOL_BRIDGE_HOST_KEY=/etc/clawbox/ssh/tool_host_ed25519
export TOOL_BRIDGE_AUTHORIZED_KEY=/etc/clawbox/ssh/runtime_ed25519.pub
exec /usr/local/bin/tool-bridge
""", encoding="utf-8")
    (args.output / "tool-init.sh").chmod(0o755)


if __name__ == "__main__":
    main()
