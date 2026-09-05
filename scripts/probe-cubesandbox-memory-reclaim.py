#!/usr/bin/env python3
"""Bounded live proof that CubeSandbox pause reclaims a sandbox's host memory."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any

from cubesandbox import Config, Sandbox


def mem_available_bytes(meminfo: Path) -> int:
    for line in meminfo.read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError(f"MemAvailable missing from {meminfo}")


def sandbox_processes(proc_root: Path, sandbox_id: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
            if sandbox_id not in command:
                continue
            rss_kib = 0
            for line in (entry / "status").read_text(encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    rss_kib = int(line.split()[1])
                    break
            matches.append({
                "pid": int(entry.name),
                "rss_bytes": rss_kib * 1024,
                "command": command.strip(),
            })
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return sorted(matches, key=lambda item: item["pid"])


def host_sample(proc_root: Path, sandbox_id: str) -> dict[str, Any]:
    processes = sandbox_processes(proc_root, sandbox_id)
    return {
        "wall_unix_s": time.time(),
        "monotonic_s": time.monotonic(),
        "metric": "host_proc_rss_and_meminfo_memavailable",
        "mem_available_bytes": mem_available_bytes(proc_root / "meminfo"),
        "sandbox_process_rss_bytes": sum(item["rss_bytes"] for item in processes),
        "sandbox_processes": processes,
    }


def run_guest(sandbox: Sandbox, command: str, timeout: int = 30) -> str:
    result = sandbox.commands.run(command, timeout=timeout)
    if result.exit_code != 0:
        raise RuntimeError(
            f"guest command failed ({result.exit_code}): {result.stderr[-1000:]}"
        )
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, help="immutable Tool template ID")
    parser.add_argument("--api-url", default="http://127.0.0.1:3000")
    parser.add_argument("--proxy-node-ip", default="127.0.0.1")
    parser.add_argument("--proxy-port", type=int, default=80)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    parser.add_argument("--allocation-mib", type=int, default=1536)
    parser.add_argument("--pause-timeout", type=float, default=120)
    parser.add_argument("--settle-seconds", type=float, default=0.5)
    parser.add_argument("--minimum-reclaimed-mib", type=float, default=256)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.allocation_mib <= 0 or args.minimum_reclaimed_mib < 0:
        parser.error("allocation and reclamation thresholds must be non-negative")

    owner = f"clawbox-memory-reclaim-probe-{uuid.uuid4().hex}"
    config = Config(
        api_url=args.api_url,
        proxy_node_ip=args.proxy_node_ip,
        proxy_port=args.proxy_port,
    )
    sandbox: Sandbox | None = None
    evidence: dict[str, Any] = {
        "schema": "clawbox_cubesandbox_memory_reclaim_v1",
        "owner": owner,
        "template_id": args.template,
        "allocation_mib": args.allocation_mib,
        "host_memory_semantics": {
            "mem_available_bytes": "whole-host Linux MemAvailable",
            "sandbox_process_rss_bytes": (
                "sum of host /proc VmRSS for processes whose cmdline contains sandbox_id"
            ),
        },
    }
    error: BaseException | None = None
    try:
        sandbox = Sandbox.create(
            args.template,
            timeout=3600,
            metadata={"clawbox-owner": owner, "clawbox-purpose": "memory-reclaim-probe"},
            config=config,
        )
        sandbox_id = sandbox.sandbox_id
        evidence["sandbox_id"] = sandbox_id
        command = (
            "nohup python3 -c \"import os,time; "
            f"x=bytearray({args.allocation_mib}*1024*1024); "
            "open('/tmp/clawbox-memory-probe','w').write(str(os.getpid())); "
            "time.sleep(1800)\" >/tmp/clawbox-memory-probe.log 2>&1 </dev/null &"
        )
        run_guest(sandbox, command)
        guest_pid = ""
        for _ in range(60):
            guest_pid = run_guest(
                sandbox,
                "test -s /tmp/clawbox-memory-probe && cat /tmp/clawbox-memory-probe || true",
            )
            if guest_pid:
                break
            time.sleep(0.25)
        if not guest_pid:
            raise RuntimeError("guest allocation process did not become ready")
        evidence["guest_pid_before_pause"] = int(guest_pid)
        time.sleep(args.settle_seconds)
        before = host_sample(args.proc_root, sandbox_id)
        pause_started = time.monotonic()
        sandbox.pause(wait=True, timeout=args.pause_timeout, interval=0.25)
        pause_seconds = time.monotonic() - pause_started
        time.sleep(args.settle_seconds)
        paused = host_sample(args.proc_root, sandbox_id)

        restore_started = time.monotonic()
        sandbox = Sandbox.connect(sandbox_id, config=config)
        guest_state = run_guest(
            sandbox,
            "p=$(cat /tmp/clawbox-memory-probe); "
            "test -d /proc/$p && echo $p; grep VmRSS /proc/$p/status",
        )
        restore_seconds = time.monotonic() - restore_started
        restored = host_sample(args.proc_root, sandbox_id)
        restored_pid = int(guest_state.splitlines()[0])
        reclaimed = paused["mem_available_bytes"] - before["mem_available_bytes"]
        process_rss_released = (
            before["sandbox_process_rss_bytes"] - paused["sandbox_process_rss_bytes"]
        )
        evidence.update({
            "pause_service_seconds": pause_seconds,
            "restore_to_guest_ready_seconds": restore_seconds,
            "before_pause": before,
            "after_pause": paused,
            "after_restore": restored,
            "guest_pid_after_restore": restored_pid,
            "guest_state_after_restore": guest_state,
            "whole_host_memavailable_reclaimed_bytes": reclaimed,
            "sandbox_process_rss_released_bytes": process_rss_released,
            "checks": {
                "sandbox_process_present_before_pause": bool(before["sandbox_processes"]),
                "sandbox_process_absent_after_pause": not paused["sandbox_processes"],
                "guest_pid_preserved": restored_pid == int(guest_pid),
                "minimum_whole_host_reclamation_observed": (
                    reclaimed >= args.minimum_reclaimed_mib * 1024 * 1024
                ),
            },
        })
        evidence["valid"] = all(evidence["checks"].values())
        if not evidence["valid"]:
            raise RuntimeError("CubeSandbox memory-reclamation evidence gate failed")
    except BaseException as exc:
        error = exc
        evidence["valid"] = False
        evidence["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup_error = None
        if sandbox is not None:
            try:
                sandbox.kill()
            except BaseException as exc:  # preserve primary failure and cleanup evidence
                cleanup_error = f"{type(exc).__name__}: {exc}"
        evidence["cleanup_error"] = cleanup_error
        evidence["cleanup_ok"] = cleanup_error is None
        rendered = json.dumps(evidence, indent=2, sort_keys=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
    if error is not None:
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
