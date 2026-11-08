"""Owned local-SSD pools and a private, aggregate-limited peer network."""
from __future__ import annotations

import ipaddress
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Literal
import uuid

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

MIB = 1024 ** 2


class LogicalNodeHostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    numa_node: int = Field(ge=0)
    cpus: str
    backing_mount: str
    filesystem_uuid: str
    image_path: str
    local_mount: str
    capacity_mib: int = Field(ge=512)
    backing_headroom_mib: int = Field(default=4096, ge=512)
    netns: str
    node_veth: str
    transfer_address: str
    transfer_port: int = Field(default=19191, ge=1024, le=65535)
    physical_device: str | None = None
    image_allocation_receipt: str | None = None
    direct_io: bool = False


class PeerNetworkSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    bandwidth_mbit_per_direction: int = Field(ge=1)
    latency_ms_one_way: float = Field(default=0, ge=0, allow_inf_nan=False)
    mtu: int = Field(default=1500, ge=576, le=9000)


class SharedDramSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    root: str
    numa_node: int = Field(default=2, ge=0)
    capacity_mib: int = Field(ge=1)
    receipt: str


class SnapshotStorageHostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[2] = 2
    topology: Literal["peer-local-ssd"] = "peer-local-ssd"
    state_root: str
    receipt: str
    nodes: dict[str, LogicalNodeHostSpec]
    network: PeerNetworkSpec
    shared_dram: SharedDramSpec

    @model_validator(mode="after")
    def valid_plan(self) -> "SnapshotStorageHostSpec":
        if set(self.nodes) != {"node0", "node1"}:
            raise ValueError("peer-local-ssd requires node0 and node1")
        paths = [self.state_root, self.receipt, self.shared_dram.root,
                 self.shared_dram.receipt]
        for node in self.nodes.values():
            paths.extend((node.image_path, node.local_mount))
            backing = PurePosixPath(node.backing_mount)
            if not backing.is_absolute() or ".." in backing.parts:
                raise ValueError("backing_mount must be an absolute path without '..'")
            if bool(node.physical_device) != bool(node.image_allocation_receipt):
                raise ValueError("physical_device and image_allocation_receipt must be configured together")
            if node.physical_device:
                paths.extend((node.physical_device, node.image_allocation_receipt))
            if not PurePosixPath(node.image_path).is_relative_to(node.backing_mount):
                raise ValueError("image_path must be inside its reviewed backing_mount")
            for name in (node.netns, node.node_veth):
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", name):
                    raise ValueError("namespace/interface names must be 1-15 safe characters")
            if not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", node.cpus):
                raise ValueError("cpus must be a Linux CPU-list")
            ipaddress.IPv4Interface(node.transfer_address)
        for value in paths:
            path = PurePosixPath(value)
            if not path.is_absolute() or ".." in path.parts or str(path) == "/":
                raise ValueError(f"host path must be absolute, non-root, without '..': {value}")
        for field in ("netns", "node_veth", "local_mount", "image_path"):
            if len({getattr(n, field) for n in self.nodes.values()}) != 2:
                raise ValueError(f"nodes must have distinct {field}")
        if len({n.filesystem_uuid for n in self.nodes.values()}) != 2:
            if not all(n.physical_device for n in self.nodes.values()) or len({n.physical_device for n in self.nodes.values()}) != 2:
                raise ValueError("shared backing filesystem requires two proven distinct physical devices")
        addresses = [ipaddress.IPv4Interface(n.transfer_address) for n in self.nodes.values()]
        if addresses[0].network != addresses[1].network or addresses[0].ip == addresses[1].ip:
            raise ValueError("peer addresses must be distinct in the same private veth subnet")
        return self


def load(path: Path) -> SnapshotStorageHostSpec:
    try:
        return SnapshotStorageHostSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read snapshot storage host plan {path}: {exc}") from exc


def init(path: Path) -> None:
    # Deliberately incomplete identities: check/apply must not guess real disks.
    plan = {
        "schema_version": 2, "topology": "peer-local-ssd",
        "state_root": "/data/clawbox-peer-host", "receipt": "/data/clawbox-peer-host/receipt.json",
        "nodes": {f"node{i}": {
            "numa_node": i, "cpus": "0-3" if i == 0 else "80-83",
            "backing_mount": f"/mnt/REVIEW-ssd{i}", "filesystem_uuid": f"REVIEW-UUID-{i}",
            "image_path": f"/mnt/REVIEW-ssd{i}/clawbox-peer/node{i}.xfs",
            "local_mount": f"/mnt/clawbox-peer-node{i}", "capacity_mib": 16384,
            "backing_headroom_mib": 4096, "netns": f"cbpeer{i}", "node_veth": f"cbpeer{i}-net",
            "transfer_address": f"10.204.0.{i + 1}/30", "transfer_port": 19191,
        } for i in range(2)},
        "network": {"bandwidth_mbit_per_direction": 400000, "latency_ms_one_way": 0, "mtu": 1500},
        "shared_dram": {"root": "/mnt/clawbox-pool", "numa_node": 2,
                        "capacity_mib": 32768, "receipt": "/REVIEW/saved-host-profile.json"},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("# Replace REVIEW identities and review all budgets before check/apply.\n")
        yaml.safe_dump(plan, stream, sort_keys=False)


def _run(*command: str, check: bool = True) -> str:
    result = subprocess.run(command, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}: {result.stderr.strip()}")
    return result.stdout.strip()


def _mount(path: str, *, containing: bool = False) -> dict[str, Any] | None:
    raw = _run("findmnt", "-J", "-T" if containing else "-M", path,
               "-o", "TARGET,SOURCE,FSTYPE,OPTIONS,UUID", check=False)
    values = json.loads(raw).get("filesystems", []) if raw else []
    return values[0] if len(values) == 1 else None


def _state_path(spec: SnapshotStorageHostSpec) -> Path:
    return Path(spec.state_root) / "state.json"


def _configuration(spec: SnapshotStorageHostSpec) -> dict:
    value = spec.model_dump(mode="json", exclude_none=True)
    for node in value["nodes"].values():
        if not node["direct_io"]:
            node.pop("direct_io")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _image_paths(spec: SnapshotStorageHostSpec) -> dict[str, Path]:
    return {key: Path(node.image_path) for key, node in spec.nodes.items()}


def _allocated_bytes(path: Path) -> int:
    return path.stat().st_blocks * 512 if path.exists() else 0


def _physical_devices(source: str) -> set[str]:
    rows = _run("lsblk", "-s", "-n", "-p", "-r", "-o", "NAME,TYPE", source).splitlines()
    return {row.split()[0] for row in rows if row.split()[-1] == "disk"}


def _cpu_ids(value: str) -> set[int]:
    result = set()
    for part in value.strip().split(","):
        bounds = [int(item) for item in part.split("-")]
        if len(bounds) == 1:
            result.add(bounds[0])
        elif bounds[0] <= bounds[1]:
            result.update(range(bounds[0], bounds[1] + 1))
        else:
            raise ValueError("CPU-list range is reversed")
    return result


def _shared_dram(spec: SnapshotStorageHostSpec) -> dict[str, Any]:
    shared = spec.shared_dram
    mount = _mount(shared.root, containing=True)
    if not mount or mount["fstype"] != "tmpfs":
        raise RuntimeError("shared DRAM is not mounted tmpfs; recover/apply the saved host plan first")
    options = set(mount["options"].split(","))
    if "noswap" not in options or f"mpol=bind:{shared.numa_node}" not in options:
        raise RuntimeError("shared DRAM must be noswap and bound to the configured NUMA node")
    if shutil.disk_usage(shared.root).total < shared.capacity_mib * MIB:
        raise RuntimeError("shared DRAM budget exceeds its actual tmpfs capacity")
    if not Path(shared.receipt).is_file():
        raise RuntimeError("saved shared-DRAM host receipt is missing; apply the saved host plan")
    return mount


def storage_configuration(spec: SnapshotStorageHostSpec) -> dict[str, Any]:
    return {"topology": spec.topology, "transport": "peer-network",
            "local_roots": {k: n.local_mount for k, n in spec.nodes.items()},
            "local_capacity_mib_by_node": {k: n.capacity_mib for k, n in spec.nodes.items()},
            "transfer_endpoints": {k: {"netns": n.netns,
                "address": str(ipaddress.IPv4Interface(n.transfer_address).ip),
                "port": n.transfer_port} for k, n in spec.nodes.items()},
            "warm_root": spec.shared_dram.root, "warm_capacity_mib": spec.shared_dram.capacity_mib}


def check(spec: SnapshotStorageHostSpec) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    def probe(name, operation, remedy):
        try:
            detail = operation()
            ok = detail is not False
        except Exception as exc:
            detail, ok = f"{type(exc).__name__}: {exc}", False
        checks.append({"check": name, "ok": ok, "detail": detail,
                       "remedy": None if ok else remedy})
    probe("root", lambda: hasattr(os, "geteuid") and os.geteuid() == 0,
          "Run scripts/clawbox --sudo experiment snapshot-storage check PLAN")
    for command in ("losetup", "mkfs.xfs", "fallocate", "mount", "umount", "findmnt", "lsblk", "ip", "tc", "ethtool", "numactl"):
        probe(command, lambda c=command: shutil.which(c) or False, f"Install the package providing {command}")
    physical: dict[str, set[str]] = {}
    for key, node in spec.nodes.items():
        def backing(k=key, n=node):
            mount = _mount(n.backing_mount)
            if not mount or mount["uuid"] != n.filesystem_uuid:
                raise RuntimeError("backing mount/UUID differs from reviewed plan; inspect findmnt/lsblk")
            if mount["fstype"] != "xfs":
                raise RuntimeError("backing must be a mounted XFS filesystem")
            physical[k] = _physical_devices(mount["source"])
            if n.physical_device:
                from .physical_storage import verify_image
                allocation = json.loads(Path(n.image_allocation_receipt).read_text())
                image = Path(n.image_path)
                if (allocation.get("status") != "verified" or allocation.get("image") != str(image)
                        or allocation.get("inode") != image.stat().st_ino
                        or allocation.get("size_bytes") != n.capacity_mib * MIB):
                    raise RuntimeError("owned image allocation receipt differs from image identity/capacity")
                proof = verify_image(image, mount["source"], n.physical_device)
                if not proof["passed"]:
                    raise RuntimeError(f"image extents escaped its physical SSD: {proof['device_bytes']}")
                recorded = allocation.get("proof") or {}
                if (recorded.get("filesystem_uuid") != proof["filesystem_uuid"]
                        or (recorded.get("physical_identity") or {}).get("serial") != proof["physical_identity"].get("serial")):
                    raise RuntimeError("backing filesystem UUID or physical SSD serial changed since allocation")
                physical[k] = {proof["physical_device"]}
                return {"mount": mount, "physical_devices": sorted(physical[k]),
                        "allocation_receipt": n.image_allocation_receipt,
                        "verified_payload_bytes": proof["logical_bytes"], "extent_count": len(proof["extents"])}
            if len(physical[k]) != 1:
                raise RuntimeError("backing must resolve to one physical SSD, not an LVM aggregate")
            return {"mount": mount, "physical_devices": sorted(physical[k])}
        probe(f"{key} backing identity", backing, "Review actual mount UUID and independent SSD topology")
        def capacity(n=node):
            image = Path(n.image_path)
            needed = max(0, n.capacity_mib * MIB - _allocated_bytes(image))
            free = shutil.disk_usage(n.backing_mount).free
            if free < needed + n.backing_headroom_mib * MIB:
                raise RuntimeError(f"free={free // MIB} MiB, additional image={needed // MIB} MiB, headroom={n.backing_headroom_mib} MiB")
            return {"free_mib": free // MIB, "additional_image_mib": needed // MIB,
                    "headroom_mib": n.backing_headroom_mib}
        probe(f"{key} backing capacity", capacity, "Reduce cases/pool size or select a verified independent SSD; preserve old data")
        def numa(n=node):
            cpulist = Path(f"/sys/devices/system/node/node{n.numa_node}/cpulist").read_text()
            if not _cpu_ids(n.cpus) <= _cpu_ids(cpulist):
                raise RuntimeError("configured CPU-list contains CPUs outside its NUMA node")
            return {"numa_node": n.numa_node, "selected_cpus": n.cpus, "node_cpus": cpulist.strip()}
        probe(f"{key} NUMA", numa, "Select CPUs actually belonging to the configured compute NUMA node")
    probe("independent physical SSDs", lambda: len(physical) == 2 and not (physical["node0"] & physical["node1"]),
          "Use distinct verified physical SSDs")
    probe("shared DRAM", lambda: _shared_dram(spec), "Recover/apply the saved shared-DRAM host plan before writing WARM data")
    return {"ready_for_apply": all(c["ok"] for c in checks),
            "already_applied": _state_path(spec).is_file(), "checks": checks,
            "proposed_plan": spec.model_dump(mode="json"),
            "proposed_storage": storage_configuration(spec)}


def _ensure_loop(image: Path, mount: str, size_mib: int,
                 state: dict[str, Any], key: str, *, direct_io: bool = False) -> None:
    record = state["loops"][key]
    if record["image"] != str(image) or record["mount"] != mount:
        raise RuntimeError("loop ownership record differs from reviewed plan")
    image.parent.mkdir(parents=True, exist_ok=True)
    Path(mount).mkdir(parents=True, exist_ok=True)
    if not image.exists():
        fd = os.open(image, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        _run("fallocate", "-l", str(size_mib * MIB), str(image))
        # -K forbids discard, which would otherwise punch out backing reservation.
        _run("mkfs.xfs", "-K", "-f", "-m", "reflink=1", str(image))
    elif record.get("preallocated_owned") and not record.get("formatted"):
        kind = _run("blkid", "-p", "-s", "TYPE", "-o", "value", str(image), check=False)
        if kind and kind != "xfs":
            raise RuntimeError("owned reserved image already contains another filesystem; refusing format")
        if not kind:
            _run("mkfs.xfs", "-K", "-f", "-m", "reflink=1", str(image))
        record["formatted"] = True
    if image.stat().st_size != size_mib * MIB or _allocated_bytes(image) < size_mib * MIB:
        raise RuntimeError(f"loop image size/reservation differs from plan: {image}; use a new owned image")
    attached = _run("losetup", "-j", str(image), check=False)
    devices = [line.split(":", 1)[0] for line in attached.splitlines()]
    mounted = _mount(mount)
    if mounted:
        if mounted["fstype"] != "xfs" or mounted["source"] not in devices:
            raise RuntimeError(f"mount is not the owned loop-XFS: {mount}")
        device = mounted["source"]
    else:
        device = devices[0] if len(devices) == 1 else _run(
            "losetup", "--find", "--show", "--direct-io=" + ("on" if direct_io else "off"), str(image))
        _run("mount", "-o", "noatime,nodiscard", device, mount)
    actual_dio = bool(int((Path("/sys/block") / Path(device).name / "loop" / "dio").read_text()))
    if actual_dio != direct_io:
        raise RuntimeError("owned loop direct I/O differs from plan; cleanup the owned mount/loop and reapply")
    record.update(device=device, allocated_bytes=_allocated_bytes(image), mount_identity=_mount(mount), direct_io=actual_dio)


def _network(spec: SnapshotStorageHostSpec, state: dict[str, Any]) -> None:
    nodes = list(spec.nodes.values())
    existing = {line.split()[0] for line in _run("ip", "netns", "list").splitlines()}
    for n in nodes:
        if n.netns not in existing:
            _run("ip", "netns", "add", n.netns)
    present = [subprocess.run(["ip", "-n", n.netns, "link", "show", n.node_veth], capture_output=True).returncode == 0 for n in nodes]
    if any(present) and not all(present):
        raise RuntimeError("only one owned peer interface exists; cleanup the owned plan and reapply")
    if not any(present):
        _run("ip", "link", "add", nodes[0].node_veth, "netns", nodes[0].netns,
             "type", "veth", "peer", "name", nodes[1].node_veth, "netns", nodes[1].netns)
    for n in nodes:
        _run("ip", "-n", n.netns, "link", "set", "lo", "up")
        _run("ip", "-n", n.netns, "addr", "replace", n.transfer_address, "dev", n.node_veth)
        _run("ip", "-n", n.netns, "link", "set", n.node_veth, "mtu", str(spec.network.mtu), "up")
        _run("ip", "netns", "exec", n.netns, "ethtool", "-K", n.node_veth,
             "tso", "off", "gso", "off", "gro", "off")
        # One egress queue per direction. No second delay or per-connection limiter.
        _run("ip", "netns", "exec", n.netns, "tc", "qdisc", "replace", "dev", n.node_veth,
             "root", "netem", "limit", "100000", "delay", f"{spec.network.latency_ms_one_way:g}ms",
             "rate", f"{spec.network.bandwidth_mbit_per_direction}mbit")
    state["network"] = {key: {"qdisc": _run("ip", "netns", "exec", n.netns, "tc", "-s", "-j", "qdisc", "show", "dev", n.node_veth),
        "link": _run("ip", "-n", n.netns, "-s", "-j", "link", "show", n.node_veth),
        "offload": _run("ip", "netns", "exec", n.netns, "ethtool", "-k", n.node_veth)} for key, n in spec.nodes.items()}


def _helper_alive(record: dict[str, Any]) -> bool:
    pid = record.get("pid")
    if not pid:
        return False
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return b"clawbox.experiments.peer_transport" in command and record["owner"].encode() in command
    except OSError:
        return False


def _helpers(spec: SnapshotStorageHostSpec, state: dict[str, Any]) -> None:
    from . import peer_transport
    implementation = hashlib.sha256(Path(peer_transport.__file__).read_bytes()).hexdigest()
    for key, node in spec.nodes.items():
        record = state.setdefault("helpers", {}).setdefault(key, {"owner": state["owner"]})
        if _helper_alive(record) and record.get("implementation_sha256") != implementation:
            for path in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    argv = path.read_bytes().split(b"\0")
                except OSError:
                    continue
                if b"clawbox.experiments.peer_transport" in argv and b"fetch" in argv:
                    raise RuntimeError("peer transfers are active; wait/abort the run before updating helpers")
            os.kill(record["pid"], signal.SIGTERM)
            deadline = time.monotonic() + 5
            while _helper_alive(record) and time.monotonic() < deadline:
                time.sleep(0.05)
            if _helper_alive(record):
                raise RuntimeError("owned old helper did not stop; inspect its log before reapplying")
        if not _helper_alive(record):
            from .peer_transport import domain_command
            log = Path(spec.state_root) / f"{key}-transfer.log"
            with log.open("ab") as output:
                process = subprocess.Popen(domain_command(node.netns, node.cpus, node.numa_node,
                    "serve", "--root", node.local_mount,
                    "--address", str(ipaddress.IPv4Interface(node.transfer_address).ip),
                    "--port", str(node.transfer_port), "--owner", state["owner"]),
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            record.update(pid=process.pid, log=str(log), implementation_sha256=implementation)
            _write_json(_state_path(spec), state)
        deadline = time.monotonic() + 10
        while True:
            completed = subprocess.run(["ip", "netns", "exec", node.netns, sys.executable,
                "-m", "clawbox.experiments.peer_transport", "ping", "--address",
                str(ipaddress.IPv4Interface(node.transfer_address).ip), "--port", str(node.transfer_port)],
                capture_output=True, text=True)
            if completed.returncode == 0:
                identity = json.loads(completed.stdout)
                if identity.get("owner") != state["owner"] or identity.get("root") != node.local_mount:
                    raise RuntimeError("peer transfer address belongs to another helper")
                locality = identity.get("locality", {})
                if (set(locality.get("cpus", [])) != _cpu_ids(node.cpus)
                        or f"bind:{node.numa_node}" not in locality.get("memory_policies", [])):
                    raise RuntimeError("peer helper CPU/NUMA policy differs from its reviewed node")
                record["identity"] = identity
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{key} transfer helper failed readiness; inspect {record['log']}")
            time.sleep(0.1)


def apply(spec: SnapshotStorageHostSpec) -> dict[str, Any]:
    active_vms = []
    for process in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            executable = Path(os.fsdecode(process.read_bytes().split(b"\0", 1)[0])).name
        except OSError:
            continue
        if executable in {"cloud-hypervisor", "containerd-shim-cube-rs"}:
            active_vms.append(int(process.parent.name))
    if active_vms:
        raise RuntimeError(f"host apply requires an idle VM pool; preserve/finish active VMs first (PIDs {active_vms})")
    preflight = check(spec)
    failures = [c for c in preflight["checks"] if not c["ok"]]
    if failures:
        raise RuntimeError("snapshot storage prerequisites failed: " + "; ".join(f"{c['check']}: {c['detail']} ({c['remedy']})" for c in failures))
    path = _state_path(spec)
    config = _configuration(spec)
    if path.exists():
        state = json.loads(path.read_text())
        if state.get("configuration") != config:
            previous = dict(state.get("configuration") or {})
            previous["network"] = config["network"]
            if previous != config:
                raise RuntimeError("owned storage identity/capacity changed; preserve old images and choose a new reviewed pool")
            # Rate/latency/MTU are experiment variables, not storage identities.
            state["configuration"] = config
    else:
        namespaces = {line.split()[0] for line in _run("ip", "netns", "list").splitlines()}
        for node in spec.nodes.values():
            if (Path(node.image_path).exists() and not node.image_allocation_receipt) or _mount(node.local_mount) or node.netns in namespaces:
                raise RuntimeError("new plan collides with existing image/mount/namespace; choose new owned names")
            if subprocess.run(["ip", "link", "show", node.node_veth], capture_output=True).returncode == 0:
                raise RuntimeError("new plan collides with an existing interface")
        state = {"schema_version": 2, "owner": uuid.uuid4().hex, "configuration": config,
                 "created_unix_s": time.time(), "loops": {key: {"image": n.image_path, "mount": n.local_mount,
                     "preallocated_owned": bool(n.image_allocation_receipt)} for key, n in spec.nodes.items()}}
        _write_json(path, state)  # Establish ownership before any formatting or namespace creation.
    for key, node in spec.nodes.items():
        _ensure_loop(Path(node.image_path), node.local_mount, node.capacity_mib, state, key, direct_io=node.direct_io)
        if node.physical_device:
            from .physical_storage import verify_image
            proof = verify_image(Path(node.image_path), _mount(node.backing_mount)["source"], node.physical_device)
            if not proof["passed"]:
                raise RuntimeError("formatted image no longer resides exclusively on its reviewed physical SSD")
            _write_json(Path(spec.state_root) / f"{key}-physical-extents.json", proof)
            state["loops"][key]["physical_extent_receipt"] = str(Path(spec.state_root) / f"{key}-physical-extents.json")
        _write_json(path, state)
    _network(spec, state)
    _helpers(spec, state)
    state["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    _write_json(path, state)
    receipt = {"schema_version": 2, "status": "applied", "applied_unix_s": time.time(),
               "boot_id": state["boot_id"], "owner": state["owner"],
               "storage": storage_configuration(spec), "host_configuration": config,
               "state": str(path), "preflight": preflight, "loops": state["loops"],
               "network": state["network"], "helpers": state["helpers"], "shared_dram_mount": _shared_dram(spec)}
    _write_json(Path(spec.receipt), receipt)
    return receipt


def cleanup(spec: SnapshotStorageHostSpec, *, execute: bool = False) -> dict[str, Any]:
    targets = {"namespaces": [n.netns for n in spec.nodes.values()],
        "local_mounts": [n.local_mount for n in spec.nodes.values()],
        "preserved_images": [n.image_path for n in spec.nodes.values()],
        "preserved_shared_dram": spec.shared_dram.root}
    if not execute:
        return {"applied": False, "preview": targets}
    if os.geteuid() != 0:
        raise ValueError("snapshot-storage cleanup --apply requires root")
    path = _state_path(spec)
    if not path.exists():
        raise RuntimeError("ownership state missing; refusing cleanup of unowned resources")
    state = json.loads(path.read_text())
    if state.get("configuration") != _configuration(spec):
        raise RuntimeError("cleanup plan differs from ownership state; use the original saved plan")
    for record in state.get("helpers", {}).values():
        if _helper_alive(record):
            os.kill(record["pid"], signal.SIGTERM)
    for key, node in spec.nodes.items():
        # umount refuses busy pools, protecting active native restore/VM references.
        mount = _mount(node.local_mount)
        if mount:
            devices = {line.split(":", 1)[0] for line in _run("losetup", "-j", node.image_path).splitlines()}
            if mount["source"] not in devices:
                raise RuntimeError("refusing to unmount unrelated storage")
            _run("umount", node.local_mount)
        for line in _run("losetup", "-j", node.image_path).splitlines():
            _run("losetup", "-d", line.split(":", 1)[0])
        _run("ip", "netns", "del", node.netns, check=False)
    # Retain ownership so repeat apply can recover the preserved images.
    state["helpers"] = {}
    state["cleaned_unix_s"] = time.time()
    _write_json(path, state)
    if Path(spec.receipt).exists():
        receipt = json.loads(Path(spec.receipt).read_text())
        receipt["status"] = "cleaned"
        _write_json(Path(spec.receipt), receipt)
    return {"applied": True, "removed": targets, "recoverable_images": targets["preserved_images"]}


def validate_peer_storage(storage) -> dict[str, Any]:
    """Bind experiment qualification to live owned pools, helpers and shaping."""
    receipt_path = Path(storage.host_receipt)
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError("peer storage receipt missing; run snapshot-storage check/apply") from exc
    if receipt.get("status") != "applied" or receipt.get("schema_version") != 2:
        raise RuntimeError("peer storage receipt is not an applied schema-2 plan")
    spec = SnapshotStorageHostSpec.model_validate(receipt["host_configuration"])
    expected = storage_configuration(spec)
    observed = {key: getattr(storage, key) for key in expected}
    observed["transfer_endpoints"] = {
        key: value.model_dump() if hasattr(value, "model_dump") else value
        for key, value in observed["transfer_endpoints"].items()}
    if not 0 < observed["warm_capacity_mib"] <= expected["warm_capacity_mib"]:
        raise RuntimeError("experiment WARM budget exceeds reviewed shared-DRAM capacity")
    observed["warm_capacity_mib"] = expected["warm_capacity_mib"]
    if observed != expected or receipt.get("storage") != expected:
        raise RuntimeError("experiment storage differs from applied peer host receipt")
    if receipt.get("boot_id") != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
        raise RuntimeError("peer receipt predates host reboot; recover saved host plan then reapply peer storage")
    failures = [row for row in check(spec)["checks"] if not row["ok"]]
    if failures:
        raise RuntimeError("peer storage host checks failed: " + json.dumps(failures))
    pools = {}
    for key, node in spec.nodes.items():
        mount = _mount(node.local_mount)
        devices = {line.split(":", 1)[0] for line in _run("losetup", "-j", node.image_path).splitlines()}
        if not mount or mount["fstype"] != "xfs" or mount["source"] not in devices:
            raise RuntimeError(f"{key} is not mounted from its owned local SSD image")
        if _allocated_bytes(Path(node.image_path)) < node.capacity_mib * MIB:
            raise RuntimeError(f"{key} backing image lost its physical reservation")
        actual_dio = bool(int((Path("/sys/block") / Path(mount["source"]).name / "loop" / "dio").read_text()))
        if actual_dio != node.direct_io:
            raise RuntimeError(f"{key} loop direct I/O differs from frozen plan; recover/apply owned storage")
        if abs(shutil.disk_usage(node.local_mount).total - node.capacity_mib * MIB) > node.capacity_mib * MIB * 0.05:
            raise RuntimeError(f"{key} mounted capacity differs from reviewed plan")
        helper = receipt["helpers"][key]
        if not _helper_alive(helper):
            raise RuntimeError(f"{key} transfer helper is unavailable; reapply peer storage")
        if set(os.sched_getaffinity(helper["pid"])) != _cpu_ids(node.cpus):
            raise RuntimeError(f"{key} helper CPU affinity changed; reapply peer storage")
        policies = {line.split()[1] for line in Path(f"/proc/{helper['pid']}/numa_maps").read_text().splitlines()
                    if len(line.split()) > 1}
        if f"bind:{node.numa_node}" not in policies:
            raise RuntimeError(f"{key} helper NUMA memory policy changed; reapply peer storage")
        actual = json.loads(_run("ip", "netns", "exec", node.netns, "tc", "-j", "qdisc", "show", "dev", node.node_veth))
        saved = json.loads(receipt["network"][key]["qdisc"])
        def shape(rows):
            # Kernel-assigned handles and random seeds are not shaping settings;
            # this plan specifies no random jitter/loss distribution.
            return [{"kind": row.get("kind"), "root": row.get("root"),
                     "options": {k: v for k, v in row.get("options", {}).items() if k != "seed"}}
                    for row in rows]
        if shape(actual) != shape(saved):
            raise RuntimeError(f"{key} shaping differs from frozen receipt; reapply peer storage")
        pools[key] = mount
    return {"receipt": str(receipt_path), "local": pools, "shared": {},
            "warm": _shared_dram(spec), "transport": "peer-network", "network": receipt["network"]}


def qualify_network(spec: SnapshotStorageHostSpec, output: Path, *, size_mib: int = 128) -> dict[str, Any]:
    """Qualify actual shared rate limiting, then restore the frozen host network.

    This deliberately reports transport qualification separately from VM results.
    The temporary low-rate test is never a formal experiment arm.
    """
    from concurrent.futures import ThreadPoolExecutor
    from .peer_transport import sparse_metadata, register_manifest
    from .spec import SnapshotSchedulingStorageSpec
    if not 16 <= size_mib <= 1024:
        raise ValueError("network qualification size must be 16..1024 MiB per object")
    runtime = SnapshotSchedulingStorageSpec(
        **storage_configuration(spec), host_receipt=spec.receipt,
        native_control_socket="/qualification-unused.sock",
    )
    live = validate_peer_storage(runtime)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write("{}\n")
    source_key, target_key = list(spec.nodes)
    source, target = spec.nodes[source_key], spec.nodes[target_key]
    token = uuid.uuid4().hex
    fixture = Path(source.local_mount) / ".clawbox-peer" / ("qualification-" + token)
    fixture.mkdir(parents=True)
    objects = []
    report = {"kind": "peer-network-qualification", "status": "running",
              "created_unix_s": time.time(), "host_receipt": spec.receipt,
              "host_configuration": spec.model_dump(mode="json"), "live": live,
              "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    state = json.loads(_state_path(spec).read_text())

    def clear_target():
        cache = Path(target.local_mount) / ".clawbox-peer" / "objects"
        for obj in objects:
            for suffix in ("", ".json", ".lock"):
                (cache / (obj["object_id"] + suffix)).unlink(missing_ok=True)

    def counters():
        return {key: json.loads(_run("ip", "-n", n.netns, "-s", "-j", "link", "show", n.node_veth))
                for key, n in spec.nodes.items()}

    def trial(label: str, duplicate: bool = False):
        clear_target()
        before = counters()
        started = time.monotonic()
        def fetch(obj):
            begin = time.monotonic()
            request = {"root": target.local_mount, "objects": [obj],
                       "address": str(ipaddress.IPv4Interface(source.transfer_address).ip),
                       "port": source.transfer_port, "timeout": 300}
            from .peer_transport import domain_command
            result = subprocess.run(domain_command(target.netns, target.cpus, target.numa_node, "fetch"),
                       input=json.dumps(request), text=True, capture_output=True, timeout=330)
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
            return {"start_monotonic_s": begin, "end_monotonic_s": time.monotonic(), **json.loads(result.stdout)}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(fetch, objects[0] if duplicate else obj) for obj in objects]
            rows = [future.result() for future in futures]
        duration = time.monotonic() - started
        payload = sum(row["payload_bytes"] for row in rows)
        after = counters()
        interface_bytes = {}
        for key in before:
            first = before[key][0].get("stats64") or before[key][0].get("stats")
            last = after[key][0].get("stats64") or after[key][0].get("stats")
            interface_bytes[key] = {direction: last[direction]["bytes"] - first[direction]["bytes"]
                                    for direction in ("rx", "tx")}
        overlap = min(row["end_monotonic_s"] for row in rows) - max(row["start_monotonic_s"] for row in rows)
        intervals = [(obj["payload_start_monotonic_s"], obj["payload_end_monotonic_s"])
                     for row in rows for obj in row["objects"]
                     if obj.get("payload_start_monotonic_s") is not None]
        payload_overlap = (max(0, min(end for _, end in intervals) - max(start for start, _ in intervals))
                           if len(intervals) > 1 else 0)
        active = peak = 0
        for _, delta in sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals]):
            active += delta
            peak = max(peak, active)
        value = {"label": label, "wall_seconds": duration, "payload_bytes": payload,
                 "protocol_bytes": sum(row["protocol_bytes"] for row in rows),
                 "interface_bytes": interface_bytes,
                 "aggregate_payload_mbit_per_second": payload * 8 / duration / 1_000_000,
                 "request_overlap_seconds": max(0, overlap),
                 "payload_overlap_seconds": payload_overlap, "active_payload_transfers_peak": peak,
                 "streams": rows,
                 "interfaces_before": before, "interfaces_after": after}
        report.setdefault("trials", []).append(value)
        _write_json(output, report)
        return value

    try:
        if shutil.disk_usage(source.local_mount).free < size_mib * MIB * 3 or shutil.disk_usage(target.local_mount).free < size_mib * MIB * 3:
            raise RuntimeError("insufficient free pool capacity for two bounded qualification objects")
        for index in range(2):
            path = fixture / f"object-{index}.bin"
            fixture_code = (
                "import os,sys,json\nfrom pathlib import Path\n"
                "from clawbox.experiments.peer_transport import process_locality\n"
                "block=os.urandom(1024**2)\n"
                "with Path(sys.argv[1]).open('xb',buffering=0) as stream:\n"
                " for _ in range(int(sys.argv[2])): stream.write(block)\n"
                " os.fsync(stream.fileno())\nprint(json.dumps(process_locality()))\n")
            locality = json.loads(_run("numactl", "--physcpubind=" + source.cpus,
                                      "--membind=" + str(source.numa_node), sys.executable,
                                      "-c", fixture_code, str(path), str(size_mib)))
            report.setdefault("fixture_locality", []).append(locality)
            objects.append({**sparse_metadata(path), "relative_path": path.name})
        register_manifest(Path(source.local_mount), {"source_root": str(fixture), "objects": objects})
        baseline = trial("configured-rate")
        # A much lower test rate makes an aggregate ceiling observable even when
        # the nominal fabric rate is above the SSD/CPU limit.
        low_rate = max(1, min(spec.network.bandwidth_mbit_per_direction,
                              int(baseline["aggregate_payload_mbit_per_second"] * 0.15)))
        low = spec.model_copy(update={"network": spec.network.model_copy(update={"bandwidth_mbit_per_direction": low_rate})})
        _network(low, state)
        report["temporary_validation_rate_mbit"] = low_rate
        report["temporary_qdisc"] = state["network"]
        limited = trial("temporary-low-rate-two-distinct-objects")
        dedup = trial("temporary-low-rate-duplicate-object", duplicate=True)
        ratio = limited["aggregate_payload_mbit_per_second"] / low_rate
        if limited["payload_overlap_seconds"] <= 0 or limited["active_payload_transfers_peak"] != 2 or not 0.50 <= ratio <= 1.20:
            raise RuntimeError(f"aggregate rate qualification failed: measured/configured={ratio:.3f}; inspect event/interface evidence")
        if dedup["payload_bytes"] != sum(r[1] for r in objects[0]["extents"]) or sum(row["cache_hits"] for row in dedup["streams"]) != 1:
            raise RuntimeError("concurrent duplicate object transferred more than once")
        report.update(status="passed", aggregate_rate_ratio=ratio)
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        try:
            _network(spec, state)
            report["restored_network"] = state["network"]
            _write_json(_state_path(spec), state)
            validate_peer_storage(runtime)
            report["restored_frozen_configuration_verified"] = True
        finally:
            clear_target()
            for obj in objects:
                (Path(source.local_mount) / ".clawbox-peer" / "registry" / (obj["object_id"] + ".json")).unlink(missing_ok=True)
            for path in fixture.iterdir():
                path.unlink()
            fixture.rmdir()
            report["owned_fixture_cleaned"] = True
            _write_json(output, report)
    return report


def cleanup_replicas(spec: SnapshotStorageHostSpec, run_id: str, control_socket: str,
                     *, execute: bool = False) -> dict[str, Any]:
    """Release native ownership first; collect only this run's unreferenced CAS.

    Native cleanup requires an idle host. Prepared templates, other runs and any
    remaining hardlink/reference protect their objects from collection.
    """
    from .peer_transport import _object_lock
    from clawbox.cube.peer_replica import native_control
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", run_id):
        raise ValueError("run ID must be a nonempty safe experiment identifier")
    if os.geteuid() != 0:
        raise ValueError("cleanup-replicas requires root to verify native references; use --sudo")
    state = json.loads(_state_path(spec).read_text())
    if state.get("configuration") != _configuration(spec):
        raise RuntimeError("cleanup plan differs from owned host state")
    health = native_control(control_socket, "/health")
    if health.get("domains") != {k: n.local_mount for k, n in spec.nodes.items()}:
        raise RuntimeError("native domain roots differ from reviewed host plan")
    active_receivers = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = path.read_bytes().split(b"\0")
            if b"clawbox.experiments.peer_transport" in argv and b"fetch" in argv:
                active_receivers.append(int(path.parent.name))
        except OSError:
            continue
    if active_receivers:
        raise RuntimeError(f"peer transfers are still active (PIDs {active_receivers}); abort/wait for the run before cleanup")
    candidates = []
    protected_objects: dict[str, set[str]] = {k: set() for k in spec.nodes}
    protected_paths: set[Path] = set()
    for key, node in spec.nodes.items():
        mount = _mount(node.local_mount)
        devices = {line.split(":", 1)[0] for line in _run("losetup", "-j", node.image_path).splitlines()}
        if not mount or mount["source"] not in devices:
            raise RuntimeError(f"{key} is not the owned mounted local SSD; recover host storage first")
        root = Path(node.local_mount).resolve()
        directory = root / ".clawbox-peer" / "replicas"
        if not directory.exists():
            continue
        for package in directory.iterdir():
            if not package.is_dir() or not package.resolve().is_relative_to(root):
                raise RuntimeError("unexpected path in owned replica directory")
            ready_path = package / "ready.json"
            manifest_path = package / "manifest.json"
            ready = json.loads(ready_path.read_text()) if ready_path.exists() else {}
            manifest = ready.get("manifest") or (json.loads(manifest_path.read_text()) if manifest_path.exists() else {})
            owners = set(ready.get("run_ids") or ([ready["run_id"]] if ready.get("run_id") else []))
            exclusive = run_id in owners and owners == {run_id}
            objects = {obj["object_id"] for obj in manifest.get("objects", [])
                       if obj.get("storage_domain") != "shared_dram"}
            if any(not re.fullmatch(r"[0-9a-f]{64}", oid) for oid in objects):
                raise RuntimeError("invalid object ID in retained replica manifest")
            if run_id in owners:
                candidates.append({"node": key, "path": str(package), "manifest_id": ready.get("manifest_id"),
                                   "exclusive": exclusive, "run_ids": sorted(owners), "objects": sorted(objects)})
            if not exclusive:
                protected_objects[key].update(objects)
                protected_paths.update(Path(value) for value in ready.get("object_paths", {}).values())
                protected_paths.update(Path(obj["source_path"]) for obj in manifest.get("objects", [])
                                       if obj.get("storage_domain") == "shared_dram" and obj.get("source_path"))
    native_preview = native_control(control_socket, "/cleanup", {"run_id": run_id, "apply": False})
    compressed_paths = set()
    warm = Path(spec.shared_dram.root).resolve()
    for catalogs in native_preview.get("catalogs", {}).values():
        for catalog in catalogs:
            if catalog.get("clawbox_run_id") != run_id or catalog.get("memory_kind") != "clawbox-direct-file":
                continue
            memory = Path(catalog["memory_vol"])
            if not memory.is_absolute() or not memory.resolve().is_relative_to(warm) or memory.resolve() == warm:
                continue
            if memory in protected_paths:
                continue
            manifest = Path(str(memory) + ".zstd-pages.json")
            directory = Path(str(memory) + ".zstd-pages")
            if not directory.resolve().is_relative_to(warm):
                raise RuntimeError("compressed generation directory escapes shared DRAM")
            if manifest.exists():
                document = json.loads(manifest.read_text())
                if any(not Path(group["container"]).resolve().is_relative_to(directory.resolve())
                       for group in document.get("groups", [])):
                    raise RuntimeError("compressed generation container escapes its owned directory")
            if manifest.exists() or directory.exists():
                compressed_paths.add(str(memory))
    report = {"run_id": run_id, "applied": False, "native": native_preview,
              "replicas": candidates, "compressed_generations": sorted(compressed_paths),
              "preserved_prepared_templates": True}
    if not execute:
        return report
    if any(not row["exclusive"] for row in candidates):
        raise RuntimeError("a native replica still belongs to another run; preserve it until those references are explicitly released")
    native_result = native_control(control_socket, "/cleanup", {"run_id": run_id, "apply": True})
    if not native_result.get("native_references_released"):
        raise RuntimeError("native cleanup did not prove references released; replica bytes are preserved")
    from clawbox.cube.snapshot_compression import remove_compressed_snapshot
    for memory in compressed_paths:
        remove_compressed_snapshot(memory)
    removed_objects, preserved_objects, removed_shared_aliases = [], [], []
    for candidate in candidates:
        package = Path(candidate["path"])
        if not candidate["exclusive"]:
            ready_path = package / "ready.json"
            ready = json.loads(ready_path.read_text())
            ready["run_ids"] = sorted(set(candidate["run_ids"]) - {run_id})
            if ready.get("run_id") == run_id:
                ready["run_id"] = ready["run_ids"][0]
            _write_json(ready_path, ready)
            continue
        shutil.rmtree(package)
        key = candidate["node"]
        manifest_id = candidate.get("manifest_id")
        if manifest_id and re.fullmatch(r"[0-9a-f]{64}", manifest_id):
            aliases = Path(spec.shared_dram.root) / ".peer-replicas" / key / manifest_id
            if aliases.exists() and not any(p.is_relative_to(aliases) for p in protected_paths):
                if not aliases.resolve().is_relative_to(Path(spec.shared_dram.root).resolve()):
                    raise RuntimeError("shared aliases escape the reviewed DRAM root")
                shutil.rmtree(aliases)
                removed_shared_aliases.append(str(aliases))
        cache = Path(spec.nodes[key].local_mount) / ".clawbox-peer" / "objects"
        for oid in candidate["objects"]:
            path = cache / oid
            if oid in protected_objects[key] or (path.exists() and path.stat().st_nlink > 1):
                preserved_objects.append(str(path))
                continue
            with _object_lock(cache / (oid + ".lock")):
                if path.exists():
                    path.unlink()
                    removed_objects.append(str(path))
                path.with_suffix(".json").unlink(missing_ok=True)
            (cache / (oid + ".lock")).unlink(missing_ok=True)
    report.update(applied=True, native=native_result, removed_objects=sorted(set(removed_objects)),
                  preserved_referenced_objects=sorted({p for p in preserved_objects if Path(p).exists()}),
                  removed_shared_aliases=removed_shared_aliases)
    return report
