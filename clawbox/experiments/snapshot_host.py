"""Reviewed host plan for the two-node snapshot scheduling study."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

MIB = 1024 ** 2


class LogicalNodeHostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    local_mount: str
    shared_mount: str
    netns: str
    host_veth: str
    node_veth: str
    host_address: str
    node_address: str


class SnapshotStorageHostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    state_root: str = "/data/clawbox-snapshot-scheduling-host"
    image_root: str = "/data/clawbox-snapshot-scheduling-host/images"
    receipt: str = "/data/clawbox-snapshot-scheduling-host/receipt.json"
    export_mount: str = "/mnt/clawbox-snapshot-shared-export"
    warm_root: str = "/mnt/clawbox-snapshot-warm"
    local_capacity_mib: int = Field(default=24576, ge=512)
    shared_capacity_mib: int = Field(default=32768, ge=512)
    warm_capacity_mib: int = Field(default=32768, ge=1)
    warm_numa_node: int = Field(default=2, ge=0)
    latency_ms: float = Field(default=5.0, ge=0, allow_inf_nan=False)
    bandwidth_mbit: int = Field(default=1000, ge=1)
    nodes: dict[str, LogicalNodeHostSpec] = Field(default_factory=lambda: {
        "node0": LogicalNodeHostSpec(
            local_mount="/mnt/clawbox-snapshot-node0",
            shared_mount="/mnt/clawbox-snapshot-shared-node0",
            netns="cbss-node0", host_veth="cbss0-host", node_veth="cbss0-node",
            host_address="10.203.0.1/30", node_address="10.203.0.2/30",
        ),
        "node1": LogicalNodeHostSpec(
            local_mount="/mnt/clawbox-snapshot-node1",
            shared_mount="/mnt/clawbox-snapshot-shared-node1",
            netns="cbss-node1", host_veth="cbss1-host", node_veth="cbss1-node",
            host_address="10.203.0.5/30", node_address="10.203.0.6/30",
        ),
    })

    @model_validator(mode="after")
    def valid_plan(self) -> "SnapshotStorageHostSpec":
        if set(self.nodes) != {"node0", "node1"}:
            raise ValueError("snapshot storage host plan requires node0 and node1")
        values = [self.state_root, self.image_root, self.receipt,
                  self.export_mount, self.warm_root]
        for node in self.nodes.values():
            values.extend((node.local_mount, node.shared_mount))
        for value in values:
            path = PurePosixPath(value)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError(f"host path must be absolute without '..': {value}")
        if len({node.netns for node in self.nodes.values()}) != 2:
            raise ValueError("network namespace names must be distinct")
        return self


def load(path: Path) -> SnapshotStorageHostSpec:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read snapshot storage host plan {path}: {exc}") from exc
    return SnapshotStorageHostSpec.model_validate(raw)


def init(path: Path) -> None:
    rendered = (
        "# Review paths, capacities, NUMA node and network shaping before apply.\n"
        + yaml.safe_dump(
            SnapshotStorageHostSpec().model_dump(mode="json"), sort_keys=False,
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(rendered)


def _run(*command: str, check: bool = True) -> str:
    completed = subprocess.run(command, text=True, capture_output=True)
    if check and completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}: "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _mount(path: str) -> dict[str, Any] | None:
    completed = subprocess.run(
        ["findmnt", "-J", "-M", path, "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        text=True, capture_output=True,
    )
    if completed.returncode:
        return None
    values = json.loads(completed.stdout).get("filesystems") or []
    return values[0] if len(values) == 1 else None


def _state_path(spec: SnapshotStorageHostSpec) -> Path:
    return Path(spec.state_root) / "state.json"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _image_paths(spec: SnapshotStorageHostSpec) -> dict[str, Path]:
    root = Path(spec.image_root)
    return {
        "node0": root / "node0.xfs",
        "node1": root / "node1.xfs",
        "shared": root / "shared.xfs",
    }


def _allocated_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    stat = path.stat()
    return getattr(stat, "st_blocks", (stat.st_size + 511) // 512) * 512


def _client_ip(address: str) -> str:
    return address.split("/", 1)[0]


def _host_identity(spec: SnapshotStorageHostSpec) -> dict[str, Any]:
    """Configuration that cannot be changed safely on an applied plan."""
    return {
        "state_root": spec.state_root,
        "image_root": spec.image_root,
        "receipt": spec.receipt,
        "export_mount": spec.export_mount,
        "warm_numa_node": spec.warm_numa_node,
        "nodes": {
            key: node.model_dump(mode="json") for key, node in spec.nodes.items()
        },
    }


def check(spec: SnapshotStorageHostSpec) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def probe(name: str, operation, remedy: str) -> None:
        try:
            detail = operation()
            ok = detail is not False
        except Exception as exc:
            detail, ok = f"{type(exc).__name__}: {exc}", False
        checks.append({"check": name, "ok": ok, "detail": detail,
                       "remedy": None if ok else remedy})

    probe("root", lambda: os.geteuid() == 0,
          "Run with: sudo bash scripts/clawbox experiment snapshot-storage check PLAN.yaml")
    for command in ("losetup", "mkfs.xfs", "mount", "umount", "findmnt",
                    "ip", "tc", "exportfs", "nsenter", "systemctl"):
        probe(command, lambda value=command: shutil.which(value) or False,
              f"Install the package providing {command}.")
    probe("NUMA node", lambda: Path(
        f"/sys/devices/system/node/node{spec.warm_numa_node}"
    ).is_dir(), "Choose an existing warm_numa_node.")
    image_parent = Path(spec.image_root)
    existing = image_parent
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    images = _image_paths(spec)
    required_bytes = sum(
        max(0, requested * MIB - _allocated_bytes(images[key]))
        for key, requested in {
            "node0": spec.local_capacity_mib,
            "node1": spec.local_capacity_mib,
            "shared": spec.shared_capacity_mib,
        }.items()
    )
    probe("image filesystem", lambda: {
        "path": str(existing), "free_mib": shutil.disk_usage(existing).free // MIB,
        "required_additional_mib": (required_bytes + MIB - 1) // MIB,
    } if shutil.disk_usage(existing).free >= required_bytes else False,
          "Select an image_root with sufficient free disk space.")
    state = None
    try:
        state = json.loads(_state_path(spec).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if state:
        for key, node in spec.nodes.items():
            probe(f"{key} local XFS", lambda n=node: _mount(n.local_mount) or False,
                  "Rerun snapshot-storage apply to restore the saved loop mount.")
            probe(f"{key} shared NFS", lambda n=node: _mount(n.shared_mount) or False,
                  "Rerun snapshot-storage apply after checking NFS and its namespace.")
        probe("shared export XFS", lambda: _mount(spec.export_mount) or False,
              "Rerun snapshot-storage apply to restore the shared loop mount.")
        probe("WARM tmpfs", lambda: _mount(spec.warm_root) or False,
              "Rerun snapshot-storage apply to restore WARM tmpfs.")
    return {
        "ready_for_apply": all(item["ok"] for item in checks),
        "already_applied": bool(state), "checks": checks,
        "proposed_plan": spec.model_dump(mode="json"),
        "proposed_storage": {
            "local_roots": {key: node.local_mount for key, node in spec.nodes.items()},
            "shared_mounts": {key: node.shared_mount for key, node in spec.nodes.items()},
            "warm_root": spec.warm_root,
            "local_capacity_mib": spec.local_capacity_mib,
            "shared_capacity_mib": spec.shared_capacity_mib,
            "warm_capacity_mib": spec.warm_capacity_mib,
        },
    }


def _ensure_loop(image: Path, mount: str, size_mib: int,
                 state: dict[str, Any], key: str) -> None:
    image.parent.mkdir(parents=True, exist_ok=True)
    Path(mount).mkdir(parents=True, exist_ok=True)
    mounted = _mount(mount)
    if mounted:
        if mounted["fstype"] != "xfs":
            raise RuntimeError(f"existing mount is not XFS: {mount}")
        attached = _run("losetup", "-j", str(image), check=False)
        devices = {
            line.split(":", 1)[0]
            for line in attached.splitlines()
            if ":" in line
        }
        if mounted["source"] not in devices:
            raise RuntimeError(
                f"existing XFS mount is not backed by the reviewed image: {mount}; "
                "choose another mount or clean up the unrelated mount explicitly"
            )
        actual = shutil.disk_usage(mount).total
        expected = size_mib * MIB
        if actual < expected * 0.95 or actual > expected * 1.05:
            raise RuntimeError(
                f"existing XFS capacity does not match the reviewed plan: {mount}; "
                "run snapshot-storage cleanup with the old plan and select a new "
                "image_root to preserve the old loop images"
            )
        state["loops"][key] = {"image": str(image), "device": mounted["source"], "mount": mount}
        return
    if not image.exists():
        _run("truncate", "-s", f"{size_mib}M", str(image))
        _run("mkfs.xfs", "-f", "-m", "reflink=1", str(image))
    elif image.stat().st_size != size_mib * MIB:
        raise RuntimeError(
            f"preserved loop image size does not match the reviewed plan: {image}; "
            "select a new image_root rather than overwriting experiment storage"
        )
    attached = _run("losetup", "-j", str(image), check=False)
    device = attached.split(":", 1)[0] if attached else _run(
        "losetup", "--find", "--show", str(image)
    )
    _run("mount", "-o", "noatime", device, mount)
    state["loops"][key] = {"image": str(image), "device": device, "mount": mount}


def _ensure_network(node: LogicalNodeHostSpec, spec: SnapshotStorageHostSpec) -> None:
    if subprocess.run(["ip", "netns", "list"], text=True, capture_output=True).stdout.find(
        node.netns
    ) < 0:
        _run("ip", "netns", "add", node.netns)
    if subprocess.run(["ip", "link", "show", node.host_veth], capture_output=True).returncode:
        _run("ip", "link", "add", node.host_veth, "type", "veth", "peer", "name", node.node_veth)
        _run("ip", "link", "set", node.node_veth, "netns", node.netns)
    _run("ip", "addr", "replace", node.host_address, "dev", node.host_veth)
    _run("ip", "link", "set", node.host_veth, "up")
    _run("ip", "netns", "exec", node.netns, "ip", "link", "set", "lo", "up")
    _run("ip", "netns", "exec", node.netns, "ip", "addr", "replace",
         node.node_address, "dev", node.node_veth)
    _run("ip", "netns", "exec", node.netns, "ip", "link", "set", node.node_veth, "up")
    netem = ["netem", "delay", f"{spec.latency_ms:g}ms", "rate", f"{spec.bandwidth_mbit}mbit"]
    _run("tc", "qdisc", "replace", "dev", node.host_veth, "root", *netem)
    _run("ip", "netns", "exec", node.netns, "tc", "qdisc", "replace",
         "dev", node.node_veth, "root", *netem)


def apply(spec: SnapshotStorageHostSpec) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise ValueError("snapshot-storage apply requires root; use scripts/clawbox --sudo")
    existing_receipt = Path(spec.receipt)
    if existing_receipt.is_file():
        receipt = json.loads(existing_receipt.read_text(encoding="utf-8"))
        proposed = {
            "local_roots": {key: node.local_mount for key, node in spec.nodes.items()},
            "shared_mounts": {key: node.shared_mount for key, node in spec.nodes.items()},
            "warm_root": spec.warm_root,
            "local_capacity_mib": spec.local_capacity_mib,
            "shared_capacity_mib": spec.shared_capacity_mib,
            "warm_capacity_mib": spec.warm_capacity_mib,
        }
        if receipt.get("storage") != proposed:
            raise RuntimeError(
                "the reviewed storage plan differs from the applied receipt; preview and "
                "apply cleanup with the old plan, then use a new image_root if capacities changed"
            )
        if receipt.get("host_configuration") != _host_identity(spec):
            raise RuntimeError(
                "the reviewed paths, NUMA binding, namespace, or addresses differ from "
                "the applied receipt; preview and apply cleanup with the old plan first"
            )
    preflight = check(spec)
    prerequisite_names = {
        "root", "losetup", "mkfs.xfs", "mount", "umount", "findmnt",
        "ip", "tc", "exportfs", "nsenter", "systemctl", "NUMA node",
        "image filesystem",
    }
    failures = [
        row for row in preflight["checks"]
        if not row["ok"] and row["check"] in prerequisite_names
    ]
    if failures:
        raise RuntimeError("snapshot storage prerequisites failed: " + ", ".join(
            row["check"] for row in failures
        ))
    state = {"schema_version": 1, "loops": {}, "created_unix_s": time.time()}
    images = _image_paths(spec)
    _ensure_loop(images["node0"], spec.nodes["node0"].local_mount,
                 spec.local_capacity_mib, state, "node0")
    _ensure_loop(images["node1"], spec.nodes["node1"].local_mount,
                 spec.local_capacity_mib, state, "node1")
    _ensure_loop(images["shared"], spec.export_mount,
                 spec.shared_capacity_mib, state, "shared")
    Path(spec.warm_root).mkdir(parents=True, exist_ok=True)
    warm = _mount(spec.warm_root)
    if warm is None:
        _run("mount", "-t", "tmpfs", "-o",
             f"size={spec.warm_capacity_mib}m,mpol=bind:{spec.warm_numa_node}",
             "clawbox-snapshot-warm", spec.warm_root)
    elif warm["fstype"] != "tmpfs":
        raise RuntimeError(f"existing WARM path is not tmpfs: {spec.warm_root}")
    else:
        if warm["source"] != "clawbox-snapshot-warm":
            raise RuntimeError(
                f"existing WARM tmpfs is not owned by this plan: {spec.warm_root}"
            )
        actual = shutil.disk_usage(spec.warm_root).total
        expected = spec.warm_capacity_mib * MIB
        if actual < expected * 0.95 or actual > expected * 1.05:
            raise RuntimeError(
                "existing WARM tmpfs size differs from the reviewed plan; run "
                "snapshot-storage cleanup before reapplying"
            )
    clients = " ".join(
        f"{_client_ip(node.node_address)}(rw,sync,no_subtree_check,no_root_squash)"
        for node in spec.nodes.values()
    )
    exports = Path("/etc/exports.d/clawbox-snapshot-scheduling.exports")
    exports.parent.mkdir(parents=True, exist_ok=True)
    exports.write_text(f"{spec.export_mount} {clients}\n", encoding="utf-8")
    _run("systemctl", "start", "rpcbind")
    _run("systemctl", "start", "nfs-server")
    _run("exportfs", "-ra")
    for node in spec.nodes.values():
        _ensure_network(node, spec)
        Path(node.shared_mount).mkdir(parents=True, exist_ok=True)
        mounted = _mount(node.shared_mount)
        server = _client_ip(node.host_address)
        if mounted is None:
            _run(
                "nsenter", f"--net=/run/netns/{node.netns}",
                "mount", "-t", "nfs", "-o",
                "vers=3,proto=tcp,nolock,hard,timeo=600,retrans=2",
                f"{server}:{spec.export_mount}", node.shared_mount,
            )
            mounted = _mount(node.shared_mount)
        expected_source = f"{server}:{spec.export_mount}"
        if (
            mounted is None
            or mounted["fstype"] not in {"nfs", "nfs4"}
            or mounted["source"] != expected_source
        ):
            raise RuntimeError(
                f"existing shared mount does not match the reviewed NFS export: "
                f"{node.shared_mount}; expected {expected_source}"
            )
    state.update({
        "exports_file": str(exports),
        "namespaces": [node.netns for node in spec.nodes.values()],
        "host_veths": [node.host_veth for node in spec.nodes.values()],
    })
    _write_json(_state_path(spec), state)
    receipt = {
        "schema_version": 1, "status": "applied", "applied_unix_s": time.time(),
        "storage": {
            "local_roots": {key: node.local_mount for key, node in spec.nodes.items()},
            "shared_mounts": {key: node.shared_mount for key, node in spec.nodes.items()},
            "warm_root": spec.warm_root,
            "local_capacity_mib": spec.local_capacity_mib,
            "shared_capacity_mib": spec.shared_capacity_mib,
            "warm_capacity_mib": spec.warm_capacity_mib,
        },
        "network": {
            key: {
                "namespace": node.netns, "host_veth": node.host_veth,
                "node_veth": node.node_veth,
                "latency_ms_each_direction": spec.latency_ms,
                "bandwidth_mbit_each_direction": spec.bandwidth_mbit,
            }
            for key, node in spec.nodes.items()
        },
        "host_configuration": _host_identity(spec),
        "state": str(_state_path(spec)),
    }
    _write_json(Path(spec.receipt), receipt)
    return receipt


def cleanup(spec: SnapshotStorageHostSpec, *, execute: bool = False) -> dict[str, Any]:
    targets = {
        "nfs_mounts": [node.shared_mount for node in spec.nodes.values()],
        "namespaces": [node.netns for node in spec.nodes.values()],
        "local_mounts": [node.local_mount for node in spec.nodes.values()],
        "shared_export_mount": spec.export_mount,
        "warm_mount": spec.warm_root,
        "exports_file": "/etc/exports.d/clawbox-snapshot-scheduling.exports",
        "preserved_images": [str(value) for value in _image_paths(spec).values()],
    }
    if not execute:
        return {"applied": False, "preview": targets}
    if os.geteuid() != 0:
        raise ValueError("snapshot-storage cleanup --apply requires root")
    state = {}
    try:
        state = json.loads(_state_path(spec).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    for node in spec.nodes.values():
        if _mount(node.shared_mount):
            _run("umount", node.shared_mount)
        _run("ip", "netns", "del", node.netns, check=False)
    exports = Path(targets["exports_file"])
    exports.unlink(missing_ok=True)
    _run("exportfs", "-ra")
    for mount in [spec.warm_root, spec.export_mount,
                  *(node.local_mount for node in spec.nodes.values())]:
        if _mount(mount):
            _run("umount", mount)
    for record in (state.get("loops") or {}).values():
        device = str(record.get("device") or "")
        if device.startswith("/dev/loop"):
            _run("losetup", "-d", device, check=False)
    Path(spec.receipt).unlink(missing_ok=True)
    _state_path(spec).unlink(missing_ok=True)
    return {"applied": True, "removed": targets, "recoverable_images": targets["preserved_images"]}
