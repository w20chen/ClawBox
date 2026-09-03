#!/usr/bin/env python3
"""Create one short-lived Cube VM and report its kprobe facilities."""
from __future__ import annotations

import argparse

from cubesandbox import NEVER_TIMEOUT, Sandbox


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True)
    parser.add_argument("--node", required=True)
    args = parser.parse_args()
    sandbox = Sandbox.create(
        template=args.template,
        timeout=NEVER_TIMEOUT,
        lifecycle={"on_timeout": "kill", "auto_resume": False},
        distribution_scope=[args.node],
        metadata={"clawbox.owner": "kprobe-diagnostic"},
    )
    try:
        command = r"""
set -x
uname -a
test -r /proc/config.gz && zcat /proc/config.gz | grep -E 'CONFIG_(KPROBES|KRETPROBES|FTRACE_SYSCALLS)=' || true
grep -w __arm64_sys_execve /proc/kallsyms || true
ls -l /sys/kernel/tracing/kprobe_events /sys/kernel/debug/tracing/kprobe_events /sys/bus/event_source/devices/kprobe/type 2>&1 || true
cat /sys/bus/event_source/devices/kprobe/type 2>&1 || true
cat /proc/sys/kernel/kptr_restrict /proc/sys/kernel/perf_event_paranoid 2>&1 || true
echo 'p:clawbox_test __arm64_sys_execve' > /sys/kernel/tracing/kprobe_events
grep clawbox_test /sys/kernel/tracing/kprobe_events
echo '-:clawbox_test' > /sys/kernel/tracing/kprobe_events
dmesg | tail -n 30
"""
        result = sandbox.commands.run(command, timeout=30)
        print(result.stdout, end="")
        print(result.stderr, end="")
        return result.exit_code
    finally:
        sandbox.kill()


if __name__ == "__main__":
    raise SystemExit(main())
