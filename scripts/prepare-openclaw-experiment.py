#!/usr/bin/env python3
"""Create fresh per-session disks for OpenClaw+ClawTune+SSH experiments."""
from __future__ import annotations
import argparse
import ipaddress
import json
import re
import shutil
import subprocess
from pathlib import Path


def run(*argv: str, quiet: bool = False) -> None:
    subprocess.run(argv, check=True, stdout=subprocess.DEVNULL if quiet else None)


def debugfs(rootfs: Path, command: str) -> None:
    run("debugfs", "-w", "-R", command, str(rootfs), quiet=True)


def inject(rootfs: Path, source: Path, destination: str, mode: str) -> None:
    debugfs(rootfs, f"write {source} {destination}")
    debugfs(rootfs, f"set_inode_field {destination} mode {mode}")


def clone_disk(source: Path, destination: Path) -> None:
    """Use copy-on-write clones on Linux; preserve the portable fallback."""
    try:
        subprocess.run(
            ["cp", "--reflink=auto", "--sparse=always", str(source), str(destination)],
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        shutil.copyfile(source, destination)


def static_ip(prefix: str, session: int, host: int) -> str:
    address = f"{prefix}.{session + 1}.{host}"
    gateway = f"{prefix}.{session + 1}.1"
    return f"ip={address}::{gateway}:255.255.255.0::eth0:off"


def parse_cpu_list(value: str) -> list[int]:
    cpus: list[int] = []
    for item in value.split(","):
        if re.fullmatch(r"\d+-\d+", item):
            first, last = (int(part) for part in item.split("-", 1))
            if last < first:
                raise ValueError("CPU range is reversed")
            cpus.extend(range(first, last + 1))
        elif item.isdigit():
            cpus.append(int(item))
        else:
            raise ValueError(f"invalid CPU-list item: {item}")
    if not cpus or len(cpus) != len(set(cpus)):
        raise ValueError("CPU list must be non-empty and unique")
    return cpus


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--sessions", type=int, default=1)
    p.add_argument("--runtime-rootfs", required=True, type=Path)
    p.add_argument("--tool-rootfs", required=True, type=Path)
    p.add_argument("--prompt", required=True, type=Path)
    p.add_argument("--model-id", required=True)
    p.add_argument("--network-prefix", default="172.30")
    p.add_argument("--runtime-memory-mib", type=int, default=2048)
    p.add_argument("--tool-memory-mib", type=int, default=4096)
    p.add_argument("--cpu-first", type=int, default=0)
    p.add_argument("--cpu-list", help="round-robin Runtime/Tool placement over this CPU list")
    p.add_argument("--numa-node", type=int, default=0)
    a = p.parse_args()
    shared_cpus = parse_cpu_list(a.cpu_list) if a.cpu_list else None
    ipaddress.ip_network(f"{a.network_prefix}.0.0/16")
    if a.sessions < 1 or a.sessions > 253:
        raise ValueError("sessions must be between 1 and 253")
    a.output.mkdir(parents=True, exist_ok=False)
    cpu_pairs = None
    if shared_cpus is not None:
        if len(shared_cpus) < 2 or len(shared_cpus) % 2:
            raise ValueError("round-robin CPU list must contain an even number of CPUs")
        cpu_pairs = [
            {"runtime": shared_cpus[index], "tool": shared_cpus[index + 1]}
            for index in range(0, len(shared_cpus), 2)
        ]
    manifest = {
        "model_id": a.model_id,
        "network_prefix": a.network_prefix,
        "sessions": [],
        "cpu_pairs": cpu_pairs,
    }
    for index in range(a.sessions):
        directory = a.output / f"session-{index:04d}"
        directory.mkdir()
        runtime_disk, tool_disk = directory / "runtime.ext4", directory / "tool.ext4"
        clone_disk(a.runtime_rootfs, runtime_disk)
        clone_disk(a.tool_rootfs, tool_disk)
        identity = directory / "id_ed25519"
        host_key = directory / "ssh_host_ed25519_key"
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(identity))
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key))
        tool_ip = f"{a.network_prefix}.{index + 1}.3"
        known_hosts = directory / "known_hosts"
        known_hosts.write_text(
            f"[{tool_ip}]:2222 {host_key.with_suffix('.pub').read_text().strip()}\n",
            encoding="utf-8",
        )
        environment = directory / "experiment.env"
        environment.write_text(
            f"EXPERIMENT_ID=session-{index:04d}\n"
            f"MODEL_BASE_URL=http://{a.network_prefix}.{index + 1}.1:18081/v1\n"
            f"MODEL_ID={a.model_id}\nRUNTIME_IP={a.network_prefix}.{index + 1}.2\n"
            f"TOOL_SSH_TARGET=executor@{tool_ip}:2222\n",
            encoding="utf-8",
        )
        for disk in (runtime_disk, tool_disk):
            debugfs(disk, "mkdir /etc/clawbox")
            debugfs(disk, "mkdir /etc/clawbox/ssh")
        inject(runtime_disk, environment, "/etc/clawbox/experiment.env", "0100644")
        inject(runtime_disk, a.prompt, "/etc/clawbox/prompt.txt", "0100644")
        inject(runtime_disk, identity, "/etc/clawbox/ssh/id_ed25519", "0100600")
        inject(runtime_disk, known_hosts, "/etc/clawbox/ssh/known_hosts", "0100644")
        inject(tool_disk, host_key, "/etc/clawbox/ssh/ssh_host_ed25519_key", "0100600")
        inject(tool_disk, identity.with_suffix(".pub"), "/etc/clawbox/ssh/id_ed25519.pub", "0100644")
        common = "console=ttyS0 reboot=k panic=1 pci=off rw"
        runtime_cpu = (
            cpu_pairs[index % len(cpu_pairs)]["runtime"]
            if cpu_pairs else a.cpu_first + index
        )
        tool_cpu = (
            cpu_pairs[index % len(cpu_pairs)]["tool"]
            if cpu_pairs else a.cpu_first + a.sessions + index
        )
        runtime_config = {
            "binary": "/opt/kata/bin/firecracker", "api_socket": str(directory / "runtime.sock"),
            "kernel_image": "/opt/kata/share/kata-containers/vmlinux.container",
            "rootfs": str(runtime_disk), "snapshot_state": str(directory / "runtime.vmstate"),
            "snapshot_memory": str(directory / "runtime.mem"), "vcpu_count": 1,
            "memory_mib": a.runtime_memory_mib,
            "boot_args": f"{common} init=/usr/local/bin/experiment-runtime-init " + static_ip(a.network_prefix, index, 2),
            "tap_device": f"crt{index:04d}", "guest_mac": f"06:30:{index + 1:02x}:00:00:02",
            "cpu_set": str(runtime_cpu), "numa_node": a.numa_node,
            "log_path": str(directory / "runtime.log"),
        }
        tool_config = {
            "binary": "/opt/kata/bin/firecracker", "api_socket": str(directory / "tool.sock"),
            "kernel_image": "/opt/kata/share/kata-containers/vmlinux.container",
            "rootfs": str(tool_disk), "snapshot_state": str(directory / "tool.vmstate"),
            "snapshot_memory": str(directory / "tool.mem"), "vcpu_count": 1,
            "memory_mib": a.tool_memory_mib,
            "boot_args": f"{common} init=/usr/local/bin/experiment-tool-init " + static_ip(a.network_prefix, index, 3),
            "tap_device": f"ctl{index:04d}", "guest_mac": f"06:30:{index + 1:02x}:00:00:03",
            "cpu_set": str(tool_cpu), "numa_node": a.numa_node,
            "log_path": str(directory / "tool.log"),
        }
        runtime_json, tool_json = directory / "runtime.json", directory / "tool.json"
        runtime_json.write_text(json.dumps(runtime_config, indent=2) + "\n")
        tool_json.write_text(json.dumps(tool_config, indent=2) + "\n")
        manifest["sessions"].append({
            "runtime": str(runtime_json), "tool": str(tool_json),
            "gateway_host": f"{a.network_prefix}.{index + 1}.1",
            "tool_host": tool_ip, "identity": str(identity),
            "known_hosts": str(known_hosts), "store": str(directory / "model-requests.json"),
        })
    (a.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__": main()
