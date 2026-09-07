#!/usr/bin/env python3
"""Check every path from rec-a's truncated filesystem probe on a fresh VM."""
import argparse
import json
import shlex
from pathlib import Path

from cubesandbox import Sandbox
from clawbox.replay.model_gateway import _REC_A_FILESYSTEM_PROBE
from clawbox.replay.trace import load_trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--template", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    ids, paths = set(), set()
    for action in load_trace(args.trace):
        if action.kind != "llm" or not isinstance(action.input, dict):
            continue
        for message in action.input.get("messages", []):
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if (function.get("name") == "exec" and
                        json.loads(function.get("arguments", "{}")) == {"command": _REC_A_FILESYSTEM_PROBE}):
                    ids.add(call["id"])
            if message.get("role") == "tool" and message.get("tool_call_id") in ids:
                paths.update(line for line in message["content"].splitlines() if line.startswith("/"))
    if not paths:
        raise ValueError("trace has no supported recorded filesystem listing")
    vm = Sandbox.create(template=args.template, timeout=300,
                        metadata={"clawbox.owner": "rec-a-listing-preflight"},
                        distribution_scope=[args.node])
    try:
        command = "\n".join(f"test -e {shlex.quote(path)} || printf '%s\\n' {shlex.quote(path)}" for path in sorted(paths))
        result = vm.commands.run(command, timeout=30)
        report = {"passed": result.exit_code == 0 and not result.stdout.strip(),
                  "template": args.template, "sandbox_id": vm.sandbox_id,
                  "checked_paths": sorted(paths), "missing_paths": result.stdout.splitlines(),
                  "stderr": result.stderr}
    finally:
        vm.kill()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
