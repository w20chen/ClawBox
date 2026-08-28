#!/usr/bin/env python3
"""Prepare isolated replay workspaces and Firecracker disks for one trial."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run(*argv: str) -> None:
    subprocess.run(argv, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sessions", required=True, type=int)
    parser.add_argument("--workspace-source", required=True, type=Path)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--rootfs-source", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--calibration", required=True, type=Path)
    parser.add_argument("--memory-mib", type=int, default=512)
    parser.add_argument("--cpu-first", type=int, default=0)
    args = parser.parse_args()
    if args.sessions < 1:
        parser.error("--sessions must be positive")
    args.output.mkdir(parents=True, exist_ok=False)

    sessions: list[dict[str, object]] = []
    for index in range(args.sessions):
        session = args.output / f"session-{index:04d}"
        workspace = session / "workspace"
        session.mkdir()
        run("git", "clone", "--quiet", "--no-hardlinks", str(args.workspace_source), str(workspace))
        run("git", "-C", str(workspace), "checkout", "--quiet", "--detach", args.base_commit)
        rootfs = session / "rootfs.ext4"
        run("cp", "--reflink=auto", "--sparse=always", str(args.rootfs_source), str(rootfs))
        config = {
            "binary": "/opt/kata/bin/firecracker",
            "api_socket": str(session / "api.sock"),
            "kernel_image": "/opt/kata/share/kata-containers/vmlinux.container",
            "rootfs": str(rootfs),
            "snapshot_state": str(session / "snapshot.vmstate"),
            "snapshot_memory": str(session / "snapshot.mem"),
            "vcpu_count": 1,
            "memory_mib": args.memory_mib,
            "boot_args": "console=ttyS0 reboot=k panic=1 pci=off rw agent.log_vport=1025",
            "cpu_set": str(args.cpu_first + index),
            "numa_node": 0,
            "log_path": str(session / "firecracker.log"),
        }
        config_path = session / "firecracker.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        sessions.append({
            "trace": str(args.trace),
            "calibration": [str(args.calibration)],
            "firecracker_config": str(config_path),
            "tool_transport": "local",
            "cwd": str(workspace),
        })
    (args.output / "manifest.json").write_text(
        json.dumps({"sessions": sessions}, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
