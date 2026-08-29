#!/usr/bin/env python3
"""Prepare isolated replay workspaces and Firecracker disks for one trial."""

from __future__ import annotations

import argparse
import ipaddress
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
    parser.add_argument(
        "--tool-rootfs-source", type=Path,
        help="optional second Firecracker rootfs for Tool-sandbox reclamation",
    )
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--calibration", required=True, type=Path)
    parser.add_argument("--memory-mib", type=int, default=512)
    parser.add_argument("--cpu-first", type=int, default=0)
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument(
        "--guest-agent", action="store_true",
        help="boot /clawbox-runtime-agent and configure a per-VM vsock endpoint",
    )
    parser.add_argument(
        "--guest-touch-mib", type=int, default=0,
        help="resident guest working set to allocate and touch (guest-agent mode)",
    )
    parser.add_argument("--tool-guest-touch-mib", type=int, default=0)
    parser.add_argument(
        "--network-prefix", metavar="A.B",
        help="use per-session bridge/TAP names and static guest IPv4 addresses under A.B.0.0/16",
    )
    parser.add_argument("--runtime-init", default="/clawbox-runtime-agent")
    parser.add_argument("--tool-init", default="/clawbox-runtime-agent")
    args = parser.parse_args()
    if args.sessions < 1:
        parser.error("--sessions must be positive")
    if args.guest_touch_mib < 0 or args.guest_touch_mib >= args.memory_mib:
        parser.error("--guest-touch-mib must be non-negative and smaller than --memory-mib")
    if args.guest_touch_mib and not args.guest_agent:
        parser.error("--guest-touch-mib requires --guest-agent")
    if args.tool_guest_touch_mib < 0 or args.tool_guest_touch_mib >= args.memory_mib:
        parser.error("--tool-guest-touch-mib must be smaller than --memory-mib")
    if args.tool_rootfs_source is not None and not args.guest_agent:
        parser.error("--tool-rootfs-source requires --guest-agent")
    if args.tool_guest_touch_mib and args.tool_rootfs_source is None:
        parser.error("--tool-guest-touch-mib requires --tool-rootfs-source")
    if args.network_prefix:
        try:
            prefix = ipaddress.ip_network(f"{args.network_prefix}.0.0/16", strict=True)
        except ValueError as exc:
            parser.error(f"--network-prefix must be two IPv4 octets: {exc}")
        if args.sessions > 253:
            parser.error("--network-prefix supports at most 253 sessions")
    else:
        prefix = None
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
            "boot_args": (
                "console=ttyS0 reboot=k panic=1 pci=off rw "
                + (
                    f"init={args.runtime_init} clawbox.touch_mib={args.guest_touch_mib}"
                    if args.guest_agent else "agent.log_vport=1025"
                )
            ),
            "cpu_set": str(args.cpu_first + index),
            "numa_node": args.numa_node,
            "log_path": str(session / "firecracker.log"),
        }
        if args.guest_agent:
            config.update({
                "vsock_uds": str(session / "runtime.vsock"),
                "guest_cid": 3 + index,
                "guest_agent_port": 18080,
            })
        if prefix is not None:
            subnet = index + 1
            config.update({
                "tap_device": f"crt{index:04d}",
                "guest_mac": f"06:30:{subnet:02x}:00:00:02",
                "boot_args": config["boot_args"] + " " + static_ip(prefix, subnet, 2),
            })
        config_path = session / "firecracker.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        session_spec: dict[str, object] = {
            "trace": str(args.trace),
            "calibration": [str(args.calibration)],
            "firecracker_config": str(config_path),
            "tool_transport": "vsock" if args.tool_rootfs_source is not None else "local",
            "cwd": str(workspace),
        }
        if args.tool_rootfs_source is not None:
            tool_rootfs = session / "tool-rootfs.ext4"
            run("cp", "--reflink=auto", "--sparse=always",
                str(args.tool_rootfs_source), str(tool_rootfs))
            tool_config = {
                "binary": "/opt/kata/bin/firecracker",
                "api_socket": str(session / "tool-api.sock"),
                "kernel_image": "/opt/kata/share/kata-containers/vmlinux.container",
                "rootfs": str(tool_rootfs),
                "snapshot_state": str(session / "tool-snapshot.vmstate"),
                "snapshot_memory": str(session / "tool-snapshot.mem"),
                "vcpu_count": 1,
                "memory_mib": args.memory_mib,
                "boot_args": (
                    "console=ttyS0 reboot=k panic=1 pci=off rw "
                    f"init={args.tool_init} "
                    f"clawbox.touch_mib={args.tool_guest_touch_mib}"
                ),
                "vsock_uds": str(session / "tool-runtime.vsock"),
                "guest_cid": 1003 + index,
                "guest_agent_port": 18080,
                "cpu_set": str(args.cpu_first + args.sessions + index),
                "numa_node": args.numa_node,
                "log_path": str(session / "tool-firecracker.log"),
            }
            if prefix is not None:
                subnet = index + 1
                tool_config.update({
                    "tap_device": f"ctl{index:04d}",
                    "guest_mac": f"06:30:{subnet:02x}:00:00:03",
                    "boot_args": tool_config["boot_args"] + " " + static_ip(prefix, subnet, 3),
                })
            tool_config_path = session / "tool-firecracker.json"
            tool_config_path.write_text(
                json.dumps(tool_config, indent=2) + "\n", encoding="utf-8",
            )
            session_spec["tool_firecracker_config"] = str(tool_config_path)
        sessions.append(session_spec)
    (args.output / "manifest.json").write_text(
        json.dumps({"sessions": sessions}, indent=2) + "\n", encoding="utf-8"
    )


def static_ip(prefix: ipaddress.IPv4Network, subnet: int, host: int) -> str:
    """Linux kernel ip= syntax; network setup happens before guest PID 1."""
    base = str(prefix.network_address).split(".")[:2]
    address = ".".join([*base, str(subnet), str(host)])
    gateway = ".".join([*base, str(subnet), "1"])
    return f"ip={address}::{gateway}:255.255.255.0::eth0:off"


if __name__ == "__main__":
    main()
