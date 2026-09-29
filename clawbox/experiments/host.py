"""Editable host plans and read-only prerequisite checks for standalone hosts."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .topology import ComputeNode, parse_cpu_list, validate_compute_nodes


class HostComputeNode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: str
    numa_node: int = Field(ge=0)
    cpus: str | None = None
    memory_gib: int = Field(default=36, gt=0)
    low_gib: int = Field(default=28, gt=0)
    high_gib: int = Field(default=32, gt=0)

    @model_validator(mode="after")
    def valid(self):
        if not self.low_gib < self.high_gib < self.memory_gib:
            raise ValueError("compute node requires LOW < HIGH < local capacity")
        if self.cpus is not None:
            parse_cpu_list(self.cpus)
        return self

    def resolved(self) -> ComputeNode:
        cpus = self.cpus or Path(f"/sys/devices/system/node/node{self.numa_node}/cpulist").read_text().strip()
        return ComputeNode(node_id=self.node_id, numa_node=self.numa_node, cpus=cpus,
                           memory_capacity_mib=self.memory_gib * 1024,
                           low_watermark_mib=self.low_gib * 1024, high_watermark_mib=self.high_gib * 1024)


class HostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    runtime_template: str = "REPLACE_RUNTIME_TEMPLATE"
    tool_template: str = "REPLACE_TOOL_TEMPLATE"
    node: str = "REPLACE_NODE"
    compute_nodes: list[HostComputeNode] = Field(default_factory=list)
    cube_source: str = "/home/you/src/CubeSandbox"
    local_node: int = Field(default=0, ge=0)
    local_gib: int = Field(default=36, gt=0)
    low_gib: int = Field(default=28, gt=0)
    high_gib: int = Field(default=32, gt=0)
    warm: bool = True
    warm_node: int = Field(default=1, ge=0)
    warm_gib: int = Field(default=128, gt=0)
    shared_borrow_percent: int = Field(default=50, ge=0, le=50)
    warm_root: str = "/mnt/clawbox-warm"
    cold_root: str = "/data/clawbox-cold"
    cubelet_data_root: str = "/data/cubelet"
    output_root: str = "/data/clawbox-results"
    minimum_disk_free_gib: int = Field(default=20, gt=0)

    @model_validator(mode="before")
    @classmethod
    def one_topology_form(cls, values):
        if isinstance(values, dict) and values.get("compute_nodes") and any(
            key in values for key in ("local_node", "local_gib", "low_gib", "high_gib")
        ):
            raise ValueError("compute_nodes replaces top-level local_node/local_gib/low_gib/high_gib; remove those fields")
        return values

    @model_validator(mode="after")
    def consistent(self):
        if self.compute_nodes:
            if not self.warm:
                raise ValueError("compute_nodes requires warm: true for the shared pool")
            if len({n.node_id for n in self.compute_nodes}) != len(self.compute_nodes) or len({n.numa_node for n in self.compute_nodes}) != len(self.compute_nodes):
                raise ValueError("compute node IDs and NUMA nodes must be unique")
            if self.warm_node in {n.numa_node for n in self.compute_nodes}:
                raise ValueError("shared pool must use a different NUMA node from every compute node")
        if self.warm and not 0 < self.low_gib < self.high_gib < self.local_gib:
            raise ValueError("require 0 < low_gib < high_gib < local_gib")
        if self.warm and not self.compute_nodes and self.local_node == self.warm_node:
            raise ValueError("LOCAL and WARM must use distinct NUMA nodes")
        for key in ("cube_source", "warm_root", "cold_root", "cubelet_data_root", "output_root"):
            value = getattr(self, key)
            if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in value.split("/"):
                raise ValueError(f"{key}: use an absolute Linux path without spaces or '..'")
            if str(Path(value)) == "/":
                raise ValueError(f"{key}: root directory is not a dedicated path")
        warm = Path(self.warm_root)
        for key in ("cold_root", "cubelet_data_root", "output_root", "cube_source"):
            other = Path(getattr(self, key))
            if warm == other or warm in other.parents or other in warm.parents:
                raise ValueError(f"warm_root must not overlap {key}")
        return self


def load_config(path: Path) -> HostConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read host configuration {path}: {exc}") from exc
    return HostConfig.model_validate(raw)


def _run(*argv: str) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"exit {result.returncode}")
    return result.stdout.strip()


def inventory() -> dict:
    nodes = []
    for path in sorted(Path("/sys/devices/system/node").glob("node[0-9]*")):
        values = {}
        for line in (path / "meminfo").read_text().splitlines():
            fields = line.split()
            if len(fields) >= 4:
                values[fields[2].rstrip(":")] = int(fields[3])
        nodes.append({"node": int(path.name[4:]), "cpus": (path / "cpulist").read_text().strip(),
                      "total_mib": values.get("MemTotal", 0) // 1024,
                      "free_mib": values.get("MemFree", 0) // 1024,
                      "distance": (path / "distance").read_text().strip()})
    return {"system": platform.system(), "architecture": platform.machine(), "numa_nodes": nodes,
            "note": "NUMA free memory excludes reclaimable cache; it is not MemAvailable or a reservation."}


def init_config(path: Path, profile_path: Path | None = None) -> None:
    values = HostConfig().model_dump()
    if not profile_path:
        values.update(compute_nodes=[HostComputeNode(node_id=f"node{i}", numa_node=i).model_dump() for i in (0, 1)],
                      warm_node=2, warm_root="/mnt/clawbox-pool")
    if profile_path:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        if profile.get("compute_nodes"):
            values["compute_nodes"] = [dict(node_id=n["node_id"], numa_node=n["numa_node"], cpus=n["cpus"],
                memory_gib=n["memory_capacity_mib"] // 1024, low_gib=n["low_watermark_mib"] // 1024,
                high_gib=n["high_watermark_mib"] // 1024) for n in profile["compute_nodes"]]
        for target, source in (("node", "node"), ("cube_source", "cube_source"),
                               ("local_node", "local_numa_node"), ("warm_node", "warm_numa_node"),
                               ("warm_root", "warm_root"), ("cold_root", "cold_root")):
            if profile.get(source) is not None:
                values[target] = profile[source]
        for target, source in (("local_gib", "local_memory_capacity_mib"),
                               ("low_gib", "local_memory_low_watermark_mib"),
                               ("high_gib", "local_memory_high_watermark_mib"),
                               ("warm_gib", "warm_capacity_mib")):
            if profile.get(source) is not None:
                if profile[source] % 1024:
                    raise ValueError(f"{source} must be whole GiB for host setup")
                values[target] = profile[source] // 1024
        values["warm"] = bool(profile.get("local_memory_high_watermark_mib"))
        capacity = profile.get("warm_capacity_mib", 0)
        borrow = profile.get("shared_memory_borrow_limit_mib", 0)
        if capacity:
            if borrow * 100 % capacity:
                raise ValueError("shared borrow must be an integer percentage for host setup")
            values["shared_borrow_percent"] = borrow * 100 // capacity
        for role, key in (("runtime", "runtime_template"), ("sandbox", "tool_template")):
            values[key] = profile[role]["template_id"]
    if values.get("compute_nodes"):
        for key in ("local_node", "local_gib", "low_gib", "high_gib"):
            values.pop(key, None)
    config = HostConfig.model_validate(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("# Edit this file, then run: clawbox experiment host check HOST.yaml\n")
        data = config.model_dump()
        if config.compute_nodes:
            for key in ("local_node", "local_gib", "low_gib", "high_gib"):
                data.pop(key)
        stream.write(yaml.safe_dump(data, sort_keys=False))


def check(config: HostConfig) -> dict:
    """Check before mutation. Never start services, refresh storage, or create VMs."""
    checks = []

    def probe(name, operation, remedy):
        try:
            detail = operation()
            ok = detail is not False
        except Exception as exc:
            ok, detail = False, str(exc)
        checks.append({"check": name, "ok": ok, "detail": detail, "remedy": None if ok else remedy})

    probe("ARM64 Linux", lambda: platform.system() == "Linux" and platform.machine() == "aarch64",
          "Run on the ARM64 Linux KVM host; see docs/installation.md step 1.")
    probe("KVM access", lambda: os.access("/dev/kvm", os.R_OK | os.W_OK),
          "Enable KVM and grant this user read/write access to /dev/kvm, then log in again.")
    probe("cgroup v2", lambda: Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
          "Boot with cgroup v2 before installing CubeSandbox.")
    probe("sudo", lambda: _run("sudo", "-n", "true"), "Run sudo -v in this terminal, then retry.")
    probe("Docker", lambda: _run("docker", "info", "--format", "{{.ServerVersion}}"),
          "Start Docker and enable this user's Docker access; docs/installation.md step 1.")
    for name in ("findmnt", "mount", "numactl", "systemctl"):
        probe(name, lambda n=name: shutil.which(n) is not None, "Install host tools from docs/installation.md step 1.")
    from clawbox.lab import SERVICES, CONTAINERS, template_record, sdk
    for unit in SERVICES:
        probe(unit, lambda u=unit: _run("systemctl", "show", u, "-p", "LoadState", "--value") == "loaded",
              "Install the patched standalone CubeSandbox release; docs/installation.md step 2.")
    for container in CONTAINERS:
        probe(container, lambda n=container: _run("docker", "inspect", "--format", "{{.Name}}", n),
              "Install/start the standalone CubeSandbox deployment; docs/installation.md step 2.")
    try:
        s3lvol_installed = _run("systemctl", "show", "cube-sandbox-s3lvol", "-p", "LoadState", "--value") == "loaded"
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        s3lvol_installed = False
    if s3lvol_installed:
        probe("S3lvol socket", lambda: Path("/var/run/s3lvol.sock").is_socket(),
              "Start the configured storage backend and cube-sandbox-s3lvol; check its journal and /data/cubelet/s3.cfg. An active supervisor without a socket is not ready.")
    probe("patched SDK source", lambda: (Path(config.cube_source) / "sdk/python/cubesandbox/sandbox.py").is_file(),
          "Set cube_source to the patched checkout from docs/installation.md step 2.")
    if config.warm:
        probe("standalone launcher configuration", lambda: _run("sudo", "-n", "test", "-f", "/usr/local/services/cubetoolbox/.one-click.env"),
              "Install the standalone release before applying WARM service settings.")
    probe("idle VM cgroup", lambda: "populated 0" in Path("/sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events").read_text().splitlines(),
          "Wait for experiments to finish, or abort/destroy their run IDs; never kill unrelated VMs.")
    probe("idle API inventory", lambda: not any(x.get("state") == "running" for x in sdk()[0].list_v2()),
          "Check CUBE_API_URL and finish/clean up existing runs before host apply.")
    topology = inventory()
    nodes = {item["node"]: item for item in topology["numa_nodes"]}
    local_domains = [(n.node_id, n.numa_node, n.memory_gib) for n in config.compute_nodes] or [("LOCAL", config.local_node, config.local_gib)]
    for label, node, gib in local_domains + (
        [("WARM", config.warm_node, config.warm_gib)] if config.warm else []
    ):
        probe(label + " physical capacity", lambda n=node, g=gib: n in nodes and nodes[n]["total_mib"] >= g * 1024,
              f"Select an existing NUMA node and a capacity below its physical RAM; requested node={node}, GiB={gib}.")
    if config.compute_nodes:
        def topology_ready():
            resolved = tuple(n.resolved() for n in config.compute_nodes)
            validate_compute_nodes(resolved, config.warm_node)
            for n in resolved:
                if not parse_cpu_list(n.cpus) <= parse_cpu_list(nodes[n.numa_node]["cpus"]):
                    raise ValueError(f"{n.node_id}: CPUs must belong to NUMA{n.numa_node}")
            return [n.model_dump() for n in resolved]
        probe("compute topology", topology_ready, "Select distinct compute NUMA nodes and non-overlapping CPU subsets belonging to those nodes.")
    for role, template in (("Runtime", config.runtime_template), ("Tool", config.tool_template)):
        def ready(t=template):
            record = template_record(t)
            if config.node not in record["nodes"]:
                raise ValueError(f"template has no READY replica on {config.node}")
            return record
        probe(role + " template", ready, "Register both templates, then fill in their IDs and node; docs/installation.md step 4.")
    for label, directory in (("Cubelet", config.cubelet_data_root), ("COLD", config.cold_root), ("results", config.output_root)):
        def disk(path=Path(directory)):
            while not path.exists() and path != path.parent:
                path = path.parent
            usage = shutil.disk_usage(path)
            if usage.free < config.minimum_disk_free_gib * 1024**3 or usage.used / usage.total >= .85:
                raise ValueError(f"{path}: free={usage.free / 1024**3:.1f} GiB, used={usage.used / usage.total:.1%}")
            if _run("findmnt", "-n", "-o", "FSTYPE", "-T", str(path)) == "tmpfs":
                raise ValueError(f"{path} is RAM-backed; choose a disk filesystem")
            return {"filesystem_path": str(path), "free_gib": round(usage.free / 1024**3, 2)}
        probe(label + " disk", disk, f"Mount/select the intended disk at {directory}; keep usage below 85% and sufficient free space. No disk is formatted automatically.")
    if config.warm:
        def warm_mount():
            path = Path(config.warm_root)
            if path.is_mount():
                options = _run("findmnt", "-n", "-o", "FSTYPE,OPTIONS", "-M", str(path))
                if not options.startswith("tmpfs ") or f"mpol=bind:{config.warm_node}" not in options.split()[1].split(","):
                    raise ValueError(options)
                if shutil.disk_usage(path).used > config.warm_gib * 1024**3:
                    raise ValueError("requested WARM capacity is smaller than existing snapshot data")
                return options
            if path.exists() and any(path.iterdir()):
                raise ValueError("nonempty unmounted directory would be hidden by tmpfs")
            return "new dedicated mount"
        probe("WARM mount", warm_mount, "Choose an empty directory or the existing tmpfs with the selected NUMA policy. Preserve old snapshots before changing nodes.")
        probe("host swap disabled", lambda: len(Path("/proc/swaps").read_text().splitlines()) == 1,
              "Disable swap on the dedicated experiment host before WARM experiments.")
    return {"ready_for_apply": all(row["ok"] for row in checks), "checks": checks, "topology": topology}


def apply(config: HostConfig, profile: Path) -> dict:
    from clawbox import lab
    report = check(config)
    if not report["ready_for_apply"]:
        print(json.dumps(report, indent=2))
        raise ValueError("host prerequisites failed; follow checks[].remedy, then rerun host check")
    domains = [n.resolved().model_dump() for n in config.compute_nodes]
    local_gib = sum(n.memory_gib for n in config.compute_nodes) if domains else config.local_gib
    local_node = config.compute_nodes[0].numa_node if domains else config.local_node
    if config.warm:
        environment = dict(CLAWBOX_LOCAL_MIB=str(local_gib * 1024),
                           CLAWBOX_WARM_MIB=str(config.warm_gib * 1024),
                           CLAWBOX_LOCAL_NODE=str(local_node), CLAWBOX_WARM_NODE=str(config.warm_node))
        if domains:
            environment["CLAWBOX_LOCAL_NODES"] = ",".join(str(n["numa_node"]) for n in domains)
        command = ["sudo", "-n", "env", *[f"{key}={value}" for key, value in environment.items()],
                   "bash", str(lab.ROOT / "scripts/setup-tiered-memory.sh"),
                   config.warm_root, config.cold_root, _run("id", "-un")]
        try:
            subprocess.run(command, check=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("host storage setup failed; inspect the preceding output, fix it and rerun host apply") from exc
        desired = {"CLAWBOX_WARM_SNAPSHOT_ROOT": config.warm_root,
                   "CLAWBOX_COLD_SNAPSHOT_ROOT": config.cold_root,
                   "CLAWBOX_KVM_DIRTY_TRACKING": "1", "CUBE_RESTORE_PRIVATE_COPY": "0"}
        if any(lab.service_setting("cube-sandbox-cubelet", key) != value for key, value in desired.items()):
            lab.command("sudo", "-n", "systemctl", "restart", "cube-sandbox-cubelet", timeout=120)
            lab.command("sudo", "-n", "systemctl", "restart", "cube-sandbox-cube-egress", timeout=120)
        for key, value in desired.items():
            if lab.service_setting("cube-sandbox-cubelet", key) != value:
                raise ValueError(f"Cubelet {key} did not become {value}; inspect the standalone launcher and service overrides")
    arguments = config.model_dump()
    arguments.update(profile=profile, local_memory_cgroup=lab.DEFAULT_LOCAL_CGROUP, compute_nodes=domains,
                     local_gib=local_gib, local_node=local_node)
    if domains:
        arguments.update(low_gib=sum(n.low_gib for n in config.compute_nodes),
                         high_gib=sum(n.high_gib for n in config.compute_nodes))
    return lab.setup(SimpleNamespace(**arguments))
