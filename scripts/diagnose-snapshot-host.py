#!/usr/bin/env python3
"""Read-only checks for a standalone CubeSandbox tiered snapshot host."""
import json
import os
from pathlib import Path
import subprocess
import sys


def command(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def main():
    if sys.platform != "linux":
        raise SystemExit("run this on the Linux CubeSandbox host")
    env = os.environ
    warm = Path(env["CLAWBOX_WARM_ROOT"]).resolve()
    cold = Path(env["CLAWBOX_COLD_ROOT"]).resolve()
    local_mib = int(env["CLAWBOX_LOCAL_MIB"])
    warm_mib = int(env["CLAWBOX_WARM_MIB"])
    local_node = env["CLAWBOX_LOCAL_NODE"]
    warm_node = env["CLAWBOX_WARM_NODE"]
    checks = []

    def check(label, ok, detail):
        checks.append({"check": label, "ok": bool(ok), "detail": str(detail)})

    check("distinct NUMA nodes and roots", local_node != warm_node and warm != cold,
          f"LOCAL={local_node} WARM={warm_node}; {warm}, {cold}")
    nodes = (read("/sys/devices/system/node/online") or "").split(",")
    for node in (local_node, warm_node):
        check(f"NUMA node {node}", Path(f"/sys/devices/system/node/node{node}").is_dir(),
              "online nodes: " + ",".join(nodes))
    rc, mount = command("findmnt", "-M", str(warm), "-n", "-o", "FSTYPE,OPTIONS")
    check("WARM tmpfs mount", rc == 0 and mount.startswith("tmpfs ")
          and f"mpol=bind:{warm_node}" in mount and "noswap" in mount, mount or "not mounted")
    if warm.is_dir():
        total = os.statvfs(warm).f_blocks * os.statvfs(warm).f_frsize
        check("WARM capacity", rc == 0 and total >= warm_mib * 1024 * 1024,
              f"mounted bytes={total}; configured MiB={warm_mib}")
    rc_cold, cold_mount = command("findmnt", "-T", str(cold), "-n", "-o", "FSTYPE")
    check("COLD disk", cold.is_dir() and rc_cold == 0 and cold_mount != "tmpfs"
          and os.access(cold, os.W_OK), f"{cold}: {cold_mount or 'unknown'}")
    if cold.is_dir():
        usage = os.statvfs(cold)
        fraction = 1 - usage.f_bavail / usage.f_blocks if usage.f_blocks else 1
        check("COLD free space", fraction < .85, f"filesystem used={fraction:.1%}; limit=85%")
    cg = Path("/sys/fs/cgroup/cube_sandbox/sandbox")
    memory_max = read(cg / "memory.max")
    check("LOCAL cgroup limit", memory_max == str(local_mib * 1024 * 1024),
          f"actual={memory_max}; configured MiB={local_mib}")
    check("LOCAL swap disabled", read(cg / "memory.swap.max") == "0",
          f"actual={read(cg / 'memory.swap.max')}")
    events = read(cg / "cgroup.events") or ""
    check("idle pool for apply", "populated 0" in events, events.replace("\n", ", "))
    rc, service = command("systemctl", "show", "cube-sandbox-cubelet.service", "-p", "Environment")
    for flag, value in {
        "CLAWBOX_KVM_DIRTY_TRACKING": "1", "CUBE_RESTORE_PRIVATE_COPY": "0",
        "CLAWBOX_WARM_SNAPSHOT_ROOT": str(warm),
        "CLAWBOX_COLD_SNAPSHOT_ROOT": str(cold),
    }.items():
        check(f"Cubelet {flag}", rc == 0 and f"{flag}={value}" in service,
              "expected " + value)
    for unit in ("cube-sandbox-cube-api.service", "cube-sandbox-cubemaster.service",
                 "cube-sandbox-cubelet.service"):
        rc, state = command("systemctl", "is-active", unit)
        check(unit, rc == 0 and state == "active", state or "unavailable")
    source = Path(env.get("CUBE_SOURCE_DIR", ""))
    if str(source) != ".":
        check("incremental source", (source / "hypervisor/vmm/src/snapshot_lineage.rs").is_file()
              and (source / "Cubelet/services/cubebox/incremental_memory.go").is_file(),
              str(source))
    report = {"schema_version": 1, "checks": checks,
              "ready_for_live_storage_validation": all(item["ok"] for item in checks)}
    print(json.dumps(report, indent=2))
    return 0 if report["ready_for_live_storage_validation"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
