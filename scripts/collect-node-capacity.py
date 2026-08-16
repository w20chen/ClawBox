#!/usr/bin/env python3
"""Read-only inventory for sizing a dedicated arm64 Firecracker node."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any


def run(*command: str) -> str:
    try:
        return subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def json_command(*command: str) -> Any:
    value = run(*command)
    try:
        return json.loads(value) if value else None
    except json.JSONDecodeError:
        return value


def mem_total() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    return 0


def kvm() -> dict[str, Any]:
    path = Path("/dev/kvm")
    if not path.exists():
        return {"present": False, "readable": False, "writable": False}
    mode = path.stat().st_mode
    return {
        "present": stat.S_ISCHR(mode),
        "readable": os.access(path, os.R_OK),
        "writable": os.access(path, os.W_OK),
    }


def devmapper() -> dict[str, Any]:
    report = json_command(
        "lvs", "--reportformat", "json", "--units", "b", "--nosuffix",
        "-o", "vg_name,lv_name,lv_size,data_percent,metadata_percent,dm_name",
    ) if shutil.which("lvs") else None
    result: dict[str, Any] = {"lvs": report, "available_bytes": 0}
    try:
        rows = report["report"][0]["lv"]
        pool = next(item for item in rows if item.get("vg_name") == "clawbox" and item.get("lv_name") == "fc-pool")
        size = int(float(pool["lv_size"]))
        used = float(pool.get("data_percent") or 100)
        result["available_bytes"] = int(size * max(0.0, 100.0 - used) / 100.0)
        result["data_percent"] = used
        result["metadata_percent"] = float(pool.get("metadata_percent") or 100)
        result["dm_name"] = pool.get("dm_name")
    except (KeyError, StopIteration, TypeError, ValueError):
        pass
    return result


def inventory() -> dict[str, Any]:
    lscpu = json_command("lscpu", "--json")
    logical = os.cpu_count() or 0
    physical = 0
    try:
        fields = {item["field"].rstrip(":"): item["data"] for item in lscpu["lscpu"]}
        sockets = int(fields.get("Socket(s)", "0"))
        cores = int(fields.get("Core(s) per socket", "0"))
        physical = sockets * cores
    except (KeyError, TypeError, ValueError):
        pass
    containerd_root = Path("/var/lib/containerd")
    containerd_usage = run("du", "-sb", str(containerd_root)) if containerd_root.exists() else ""
    return {
        "architecture": os.uname().machine,
        "kernel": os.uname().release,
        "cpu": {"logical": logical, "physical": physical, "lscpu": lscpu},
        "target": {"logical_cpu_cores": 320, "logical_cpu_target_met": logical >= 320},
        "numa": run("numactl", "--hardware") if shutil.which("numactl") else "numactl unavailable",
        "memory_bytes": mem_total(),
        "block_devices": json_command("lsblk", "-J", "-b", "-e7", "-o", "NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL"),
        "root_mount": json_command("findmnt", "-J", "/"),
        "kvm": kvm(),
        "network": json_command("ip", "-j", "address"),
        "containerd_storage_bytes": int(containerd_usage.split()[0]) if containerd_usage else 0,
        "devmapper": devmapper(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configmap", action="store_true", help="emit the controller capacity ConfigMap instead of JSON")
    args = parser.parse_args()
    value = inventory()
    if args.configmap:
        if value["architecture"] not in {"aarch64", "arm64"}:
            raise SystemExit(f"native arm64 capacity is required; found {value['architecture']}")
        if not value["kvm"]["present"] or not value["kvm"]["readable"] or not value["kvm"]["writable"]:
            raise SystemExit("usable /dev/kvm is required before emitting capacity")
        available = int(value["devmapper"]["available_bytes"])
        if available <= 0:
            raise SystemExit("a healthy clawbox/fc-pool is required before emitting capacity")
        if not shutil.which("kubectl"):
            raise SystemExit("kubectl is required to prove that no Cell reservation is active")
        tasks = json_command("kubectl", "get", "sandboxtasks", "--all-namespaces", "-o", "json")
        if not isinstance(tasks, dict) or not isinstance(tasks.get("items"), list):
            raise SystemExit("could not inventory SandboxTasks; capacity baseline was not emitted")
        active = [
            item.get("metadata", {}).get("name", "unknown")
            for item in tasks["items"]
            if item.get("status", {}).get("phase") != "Cleaned"
        ]
        if active:
            raise SystemExit("active SandboxTasks prevent a clean capacity baseline: " + ", ".join(active))
        print("""apiVersion: v1
kind: ConfigMap
metadata:
  name: clawbox-node-capacity
  namespace: clawbox-system
data:
  devmapper-available-bytes: "%d"
""" % available)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
