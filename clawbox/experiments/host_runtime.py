"""Daily operations for an installed standalone CubeSandbox experiment host."""
from __future__ import annotations

import argparse
import inspect
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("cube-sandbox-cubeops", "cube-sandbox-cubemaster",
            "cube-sandbox-cube-api", "cube-sandbox-cubelet")
CONTAINERS = ("cube-sandbox-mysql", "cube-sandbox-redis", "cube-sandbox-minio",
              "cube-proxy-coredns", "cube-proxy", "cube-egress", "cube-lifecycle-manager")
DEFAULT_POOL_GIB = 64
DEFAULT_WARM_GIB = 128
DEFAULT_CHECKPOINT_HEADROOM_GIB = 8
DEFAULT_LOCAL_CGROUP = "/sys/fs/cgroup/cube_sandbox/sandbox"


def _write_json_atomic(path: Path, value: dict) -> None:
    """Publish one complete profile while preserving the previous file on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot_sdk_info() -> dict:
    """Inspect the installed SDK in a fresh interpreter without caching imports."""
    program = r"""
import inspect, json
from pathlib import Path
from cubesandbox import Sandbox
import cubesandbox.sandbox as module
pause = set(inspect.signature(Sandbox.pause).parameters)
connect = set(inspect.signature(Sandbox.connect).parameters)
required_pause = {
    "snapshot_tier", "memory_snapshot_path", "snapshot_generation",
    "snapshot_mechanism",
}
bounded_control = all(
    "timeout=cfg.request_timeout" in inspect.getsource(method)
    for method in (Sandbox.create, Sandbox.list_v2, Sandbox.connect)
) and "timeout=self._config.request_timeout" in inspect.getsource(Sandbox._build_session)
print(json.dumps({
    "file": str(Path(inspect.getfile(module)).resolve()),
    "pause": sorted(pause),
    "connect": sorted(connect),
    "relocate": hasattr(Sandbox, "relocate_snapshot"),
    "bounded_control": bounded_control,
    "ready": required_pause <= pause and "snapshot_mechanism" in connect
             and hasattr(Sandbox, "relocate_snapshot") and bounded_control,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True,
        timeout=30,
    )
    if result.returncode:
        return {"ready": False, "file": None, "error": result.stderr.strip()}
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("CubeSandbox SDK inspection returned invalid output") from exc
    if not isinstance(value, dict):
        raise RuntimeError("CubeSandbox SDK inspection returned a non-object")
    return value


def _sdk_source_from_module(module_file: str | None) -> Path | None:
    if not module_file:
        return None
    path = Path(module_file).resolve()
    expected = Path("sdk/python/cubesandbox/sandbox.py").parts
    if len(path.parts) < len(expected) or path.parts[-len(expected):] != expected:
        return None
    return path.parents[3]


def ensure_snapshot_sdk(cube_source: str | Path | None = None) -> Path | None:
    """Install the exact tiered/incremental SDK required by WARM experiments."""
    info = _snapshot_sdk_info()
    explicit = Path(cube_source).expanduser().resolve() if cube_source else None
    inferred = _sdk_source_from_module(info.get("file"))
    source = explicit or inferred
    if info.get("ready"):
        if explicit and not (explicit / "sdk/python/cubesandbox/sandbox.py").is_file():
            raise ValueError(f"CubeSandbox source lacks its Python SDK: {explicit}")
        return source
    if source is None:
        raise RuntimeError(
            "WARM mode requires the patched CubeSandbox SDK; pass --cube-source "
            "PATH (or set CUBE_SOURCE_DIR) so setup can install it"
        )
    sdk_file = source / "sdk/python/cubesandbox/sandbox.py"
    if not (source / ".git").exists() or not sdk_file.is_file():
        raise ValueError(
            f"CubeSandbox source must be a Git checkout containing sdk/python: {source}"
        )
    source_text = sdk_file.read_text(encoding="utf-8")
    patches = (
        ("def relocate_snapshot", ROOT / "deploy/cubesandbox/tiered-memory-api.patch"),
        ("snapshot_mechanism: str | None", ROOT / "deploy/cubesandbox/incremental-cow-snapshot.patch"),
        ('headers={"Content-Type": "application/json", **_auth_headers(cfg)},\n                      timeout=cfg.request_timeout)',
         ROOT / "deploy/cubesandbox/sdk-control-timeouts.patch"),
        ('timeout=self._config.request_timeout',
         ROOT / "deploy/cubesandbox/sdk-lifecycle-timeouts.patch"),
    )
    for marker, patch in patches:
        if marker in source_text:
            continue
        include = "sdk/python/cubesandbox/sandbox.py"
        command("git", "-C", str(source), "apply", "--check", f"--include={include}", str(patch))
        command("git", "-C", str(source), "apply", f"--include={include}", str(patch))
        source_text = sdk_file.read_text(encoding="utf-8")
    command(
        sys.executable, "-m", "pip", "install", "--no-deps",
        "--no-build-isolation", "-e", str(source / "sdk/python"), timeout=120,
    )
    verified = _snapshot_sdk_info()
    if not verified.get("ready"):
        detail = verified.get("error") or verified
        raise RuntimeError(f"patched CubeSandbox SDK is still unavailable: {detail}")
    # Editable-install path files are loaded only when an interpreter starts.
    # Setup must use the freshly installed SDK later in this same process.
    sdk_path = str((source / "sdk/python").resolve())
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)
        importlib.invalidate_caches()
    return source


def command(*args: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Command timed out after {timeout}s: {' '.join(args)}"
        ) from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Command failed: {' '.join(args)} (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return result.stdout.strip()


def sdk():
    from cubesandbox import Sandbox, Template
    return Sandbox, Template


def service_setting(unit: str, key: str) -> str | None:
    pid = int(command("systemctl", "show", unit, "--property=MainPID", "--value"))
    if pid <= 0:
        return None
    entries = command("sudo", "-n", "cat", f"/proc/{pid}/environ").split("\0")
    return next((entry.split("=", 1)[1] for entry in entries if entry.startswith(key + "=")), None)


def refresh_snapshot_storage(profile: dict) -> bool:
    """Refresh CubeMaster's node storage cache and require a writable mode."""
    try:
        # Use the same local endpoint as `storage status --refresh`. Starting
        # cubemastercli enumerates host interfaces before its RPC timeout starts;
        # with thousands of TAPs this can consume the entire subprocess timeout.
        # Loopback control-plane traffic must not use a machine HTTP proxy.
        with build_opener(ProxyHandler({})).open(
            "http://127.0.0.1:8089/cube/snapshot/storage?refresh=true", timeout=10,
        ) as response:
            payload = json.load(response)
    except (ValueError, URLError, OSError):
        return False
    if not isinstance(payload, dict):
        return False
    ret = payload.get("ret")
    if not isinstance(ret, dict) or int(ret.get("ret_code", -1)) != 200:
        return False
    target = str(profile.get("node") or "").strip()
    records = payload.get("data")
    if not target or not isinstance(records, list):
        return False
    for record in records:
        if not isinstance(record, dict):
            continue
        node_ids = {str(record.get("node_id") or ""), str(record.get("node_ip") or "")}
        if target in node_ids:
            return str(record.get("mode") or "").lower() in {"healthy", "warn"}
    return False


def wait_for_snapshot_storage(profile: dict, *, timeout: float = 600) -> None:
    """A running systemd wrapper does not imply Cubelet has begun heartbeats."""
    deadline = time.monotonic() + timeout
    next_notice = 0.0
    while not refresh_snapshot_storage(profile):
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError(
                f"Node {profile['node']} snapshot storage was not writable within {timeout:g}s. "
                "Check Cubelet startup/heartbeats, S3lvol socket/backend and data-disk space; "
                "rerun host apply after fixing the cause. Profile not saved."
            )
        if now >= next_notice:
            print(f"Waiting for node {profile['node']} storage/heartbeat readiness "
                  f"({deadline - now:.0f}s remaining)...", flush=True)
            next_notice = now + 30
        time.sleep(min(2, deadline - now))


def wait_for_vm_ready(profile: dict) -> None:
    from cubesandbox import Sandbox, ApiError
    deadline = time.monotonic() + 600
    print("Checking VM creation and guest execution (node startup can take several minutes)...", flush=True)
    while True:
        # CubeMaster loses this in-memory cache across restarts. Refresh it
        # before scheduling so the first create does not fail with a stale
        # "snapshot storage unavailable" locality decision.
        refresh_snapshot_storage(profile)
        try:
            probe = Sandbox.create(template=profile["sandbox"]["template_id"], timeout=120,
                                   lifecycle={"on_timeout": "kill", "auto_resume": False},
                                   distribution_scope=[profile["node"]],
                                   metadata={"clawbox.owner": "lab-setup-" + uuid.uuid4().hex,
                                       **({'clawbox.storage.domain': sorted(profile['snapshot_scheduling_storage']['local_roots'])[0]}
                                          if (profile.get('snapshot_scheduling_storage') or {}).get('topology') == 'peer-local-ssd' else {})})
            break
        except ApiError as exc:
            # Retry only a definitive rejection; never repeat ambiguous creates.
            rejected = ("error code 130597:" in str(exc) or
                        ("error code 130400:" in str(exc) and "not ready on any healthy node" in str(exc)))
            if not rejected or time.monotonic() >= deadline:
                raise
            print("Waiting for Cube node resources...", flush=True)
            time.sleep(10)
    try:
        result = probe.commands.run("printf clawbox-ready", timeout=30)
        if result.exit_code or result.stdout != "clawbox-ready":
            raise RuntimeError("Created VM did not pass guest execution check")
    finally:
        probe.kill()
    print("OK  VM creation, execution and destruction", flush=True)


def template_record(template_id: str) -> dict:
    _, Template = sdk()
    item = Template.get(template_id)
    if str(item.status).lower() != "ready":
        raise ValueError(f"Template {template_id} is not READY")
    image = str(item.image_info or "")
    if not re.search(r"@sha256:[0-9a-f]{64}$", image):
        raise ValueError(f"Template {template_id} lacks an immutable image digest")
    containers = (item.create_request or {}).get("containers") or []
    resources = containers[0].get("resources", {}) if containers else {}
    mem = str(resources.get("mem", ""))
    cpu = str(resources.get("cpu", ""))
    if not re.fullmatch(r"\d+Mi", mem) or not re.fullmatch(r"\d+m", cpu):
        raise ValueError(f"Template {template_id} has unsupported resource units")
    return {"template_id": template_id, "source_image_reference": image,
            "image_digest": image.split("@", 1)[1], "memory_mib": int(mem[:-2]),
            "vcpu": max(1, (int(cpu[:-1]) + 999) // 1000),
            "nodes": [r["node_id"] for r in item.replicas or [] if r.get("phase") == "READY"]}


def apply_environment(profile: dict) -> None:
    for key in ("CUBE_NODE", "CLAWBOX_CONTROL_HOST", "CLAWBOX_MODEL_GATEWAY_HOST"):
        os.environ.setdefault(key, profile["node"])
    os.environ.setdefault("CUBE_API_URL", "http://127.0.0.1:3000")
    hosts = ["localhost", "127.0.0.1", profile["node"]]
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = ",".join(filter(None, [os.environ.get(key, ""), *hosts]))


def doctor(profile: dict, *, warm: bool = False, current_images: bool = False) -> list[str]:
    failures = []
    checks = [("KVM access", lambda: os.access("/dev/kvm", os.R_OK | os.W_OK)),
              ("cgroup v2", lambda: Path("/sys/fs/cgroup/cgroup.controllers").exists()),
              ("disk space >= 5 GiB", lambda: shutil.disk_usage(ROOT).free >= 5 * 1024**3)]
    if profile.get('cube_capacity_demand'):
        from .cube_capacity import require_capacity
        checks.insert(0, ('Cube configured-VM capacity', lambda: require_capacity(profile)))
    for module in ("jsonschema", "cryptography", "yaml", "httpx"):
        checks.append(("Python " + module, lambda m=module: bool(importlib.import_module(m))))
    for unit in SERVICES:
        checks.append((unit, lambda u=unit: command("systemctl", "is-active", u) == "active"))
    for name in CONTAINERS:
        checks.append((name, lambda n=name: command("docker", "inspect", "--format", "{{.State.Running}}", n) == "true"))
    for role in ("runtime", "sandbox"):
        checks.append((role + " template", lambda r=role: bool(template_record(profile[r]["template_id"]))))
    if profile.get("local_memory_cgroup"):
        local = Path(profile["local_memory_cgroup"])
        borrow_mib = int(profile.get("shared_memory_borrow_limit_mib") or 0)
        domains = profile.get("compute_nodes") or []
        expected_nodes = {int(n["numa_node"]) for n in domains} if domains else {int(profile["local_numa_node"])}
        if profile.get("shared_memory_borrow_limit_mib") is not None:
            expected_nodes.add(int(profile["warm_numa_node"]))
        def cpuset_nodes() -> set[int]:
            nodes: set[int] = set()
            for part in (local / "cpuset.mems.effective").read_text().strip().split(","):
                start, separator, end = part.partition("-")
                nodes.update(
                    range(int(start), int(end) + 1) if separator else (int(start),)
                )
            return nodes
        checks.extend([
            ("combined live memory.max", lambda: (local/"memory.max").read_text().strip()
             == str((profile["local_memory_capacity_mib"] + borrow_mib) * 1024 * 1024)),
            ("LOCAL/shared NUMA nodes", lambda: cpuset_nodes() == expected_nodes),
            ("LOCAL swap disabled", lambda: (local/"memory.swap.max").read_text().strip() == "0"),
        ])
        if domains:
            from .topology import parse_cpu_list
            checks.append(("compute CPU set", lambda: set().union(
                *(parse_cpu_list(n["cpus"]) for n in domains)
            ) <= parse_cpu_list((local / "cpuset.cpus.effective").read_text().strip())))
            for node in domains:
                checks.append((f"{node['node_id']} CPUs belong to NUMA{node['numa_node']}",
                    lambda n=node: parse_cpu_list(n["cpus"]) <= parse_cpu_list(
                        Path(f"/sys/devices/system/node/node{n['numa_node']}/cpulist").read_text().strip())))
    if current_images:
        from clawbox.clawtune_integration import source_revision
        revision = source_revision(Path(os.environ["CLAWTUNE_ROOT"]))
        def matches(role):
            item = sdk()[1].get(profile[role]["template_id"])
            containers = (item.create_request or {}).get("containers") or []
            env = {e["key"]: e.get("value") for c in containers for e in c.get("envs", [])}
            return revision != "unknown" and env.get("CLAWTUNE_REVISION") == revision
        for role in ("runtime", "sandbox"):
            checks.append((
                role + " current ClawTune (update with clawbox experiment images)",
                lambda r=role: matches(r),
            ))
    if warm:
        for unit in ("cube-sandbox-cubemaster", "cube-sandbox-cubelet"):
            checks.append((unit + " WARM root", lambda u=unit: service_setting(u, "CLAWBOX_WARM_SNAPSHOT_ROOT") == profile["warm_root"]))
        for key, value in (("CLAWBOX_KVM_DIRTY_TRACKING", "1"), ("CUBE_RESTORE_PRIVATE_COPY", "0")):
            checks.append((key, lambda k=key, v=value: service_setting("cube-sandbox-cubelet", k) == v))
        checks.append(("WARM is tmpfs", lambda: command("findmnt", "-n", "-o", "FSTYPE", "-M", profile["warm_root"]) == "tmpfs"))
        checks.append(("WARM capacity", lambda: shutil.disk_usage(profile["warm_root"]).total
                       >= profile["warm_capacity_mib"] * 1024 * 1024))
        if profile.get("warm_numa_node") is not None:
            checks.append(("WARM NUMA node", lambda: f"mpol=bind:{profile['warm_numa_node']}"
                           in command("findmnt", "-n", "-o", "OPTIONS", "-M", profile["warm_root"])))
        def snapshot_sdk_ready() -> bool:
            sandbox = sdk()[0]
            pause = set(inspect.signature(sandbox.pause).parameters)
            connect = set(inspect.signature(sandbox.connect).parameters)
            return (
                {"snapshot_tier", "memory_snapshot_path", "snapshot_generation",
                 "snapshot_mechanism"} <= pause
                and "snapshot_mechanism" in connect
                and hasattr(sandbox, "relocate_snapshot")
            )
        checks.append(("tiered incremental SDK", snapshot_sdk_ready))
        checks.append(("host swap disabled", lambda: len(Path("/proc/swaps").read_text().splitlines()) == 1))
        checks.append(("snapshot storage writable", lambda: refresh_snapshot_storage(profile)))
    for name, check in checks:
        detail = ""
        try:
            ok = bool(check())
        except Exception as exc:
            ok = False
            detail = f": {exc}"
        print(f"{'OK' if ok else 'FAIL'}  {name}{detail}", flush=True)
        if not ok:
            failures.append(name)
    return failures


def setup(args) -> dict:
    domains = getattr(args, "compute_nodes", None) or []
    # Reject invalid requests before starting services or touching cgroups.
    low_gib = args.low_gib if args.low_gib is not None else args.local_gib - 8
    high_gib = args.high_gib if args.high_gib is not None else args.local_gib - 4
    if args.local_gib <= 0 or args.local_node < 0:
        raise ValueError("LOCAL capacity must be positive and NUMA node non-negative")
    if args.local_memory_cgroup != DEFAULT_LOCAL_CGROUP:
        raise ValueError(f"standalone CubeSandbox requires --local-memory-cgroup {DEFAULT_LOCAL_CGROUP}")
    if args.warm:
        if not (0 < low_gib < high_gib < args.local_gib):
            raise ValueError("memory watermarks must satisfy 0 < LOW < HIGH < LOCAL hard")
        if args.warm_gib <= 0 or args.warm_node < 0 or args.local_node == args.warm_node:
            raise ValueError("WARM capacity must be positive and use a distinct non-negative NUMA node")
        if not Path(args.warm_root).is_absolute() or Path(args.warm_root) == Path("/"):
            raise ValueError("WARM root must be an absolute dedicated directory")
    if not 0 <= args.shared_borrow_percent <= 100:
        raise ValueError("--shared-borrow-percent must be between 0 and 100")
    if not Path("/dev/kvm").exists():
        raise ValueError("Run setup on the Linux KVM host, not the Windows client")
    if shutil.disk_usage(ROOT).free < 5 * 1024**3:
        raise ValueError("Less than 5 GiB free. Free disk space before starting services; setup never deletes old data")
    previous = json.loads(args.profile.read_text(encoding="utf-8")) if args.profile.exists() else {}
    if not isinstance(previous, dict):
        raise ValueError("Host profile root must be a JSON object")
    runtime = args.runtime_template or previous.get("runtime", {}).get("template_id") or os.getenv("CLAWBOX_RUNTIME_TEMPLATE")
    tool = args.tool_template or previous.get("sandbox", {}).get("template_id") or os.getenv("CLAWBOX_TOOL_TEMPLATE")
    if not runtime or not tool:
        raise ValueError("Supply --runtime-template and --tool-template, or export them from machine.env")
    cube_source = (
        getattr(args, "cube_source", None) or previous.get("cube_source")
        or os.getenv("CUBE_SOURCE_DIR")
    )
    if args.warm:
        resolved_source = ensure_snapshot_sdk(cube_source)
        if resolved_source is not None:
            cube_source = str(resolved_source)
    # Start only the installed CubeSandbox deployment, never unrelated containers.
    command("sudo", "-n", "systemctl", "start", "containerd", "docker", timeout=120)
    for name in CONTAINERS:
        command("docker", "start", name, timeout=120)
        deadline = time.monotonic() + 90
        while True:
            health = command("docker", "inspect", "--format",
                             "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", name)
            if health in {"healthy", "running"}:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{name} did not become healthy; inspect docker logs {name}")
            time.sleep(1)
    for unit in SERVICES:
        command("sudo", "-n", "systemctl", "start", unit, timeout=120)
    profile = {"runtime": template_record(runtime), "sandbox": template_record(tool)}
    nodes = set(profile["runtime"].pop("nodes")) & set(profile["sandbox"].pop("nodes"))
    node = args.node or previous.get("node") or os.getenv("CUBE_NODE")
    if node is None and len(nodes) == 1:
        node = nodes.pop()
        nodes.add(node)
    if node not in nodes:
        raise ValueError("Select a node with READY replicas of both templates using --node")
    shared_borrow_mib = (
        args.warm_gib * 1024 * args.shared_borrow_percent // 100 if args.warm else 0
    )
    profile.update(
        node=node,
        local_memory_cgroup=args.local_memory_cgroup,
        local_memory_capacity_mib=args.local_gib * 1024,
        local_numa_node=None if domains else args.local_node,
        compute_nodes=domains,
        local_memory_low_watermark_mib=low_gib * 1024 if args.warm or domains else None,
        local_memory_high_watermark_mib=high_gib * 1024 if args.warm or domains else None,
        shared_memory_borrow_limit_mib=shared_borrow_mib if args.warm else None,
        warm_root=args.warm_root if args.warm else None,
        warm_capacity_mib=args.warm_gib * 1024 if args.warm else 0,
        warm_numa_node=args.warm_node if args.warm else None,
        snapshot_compression_binary=getattr(
            args, "snapshot_compression_binary",
            "/usr/local/bin/clawbox-zstd-page-snapshot",
        ),
    )
    if cube_source:
        profile["cube_source"] = str(Path(cube_source).expanduser().resolve())
    for key in ("cold_root", "output_root"):
        value = getattr(args, key, None) or previous.get(key)
        if value:
            profile[key] = value
    if isinstance(previous.get("image_build"), dict):
        profile["image_build"] = previous["image_build"]
    apply_environment(profile)
    if any(x.get("state") == "running" for x in sdk()[0].list_v2()):
        raise ValueError("Lab setup requires an idle VM host; existing VMs will not be stopped")
    command(
        "sudo", "-n", sys.executable, str(ROOT/"scripts/configure-tiered-local.py"),
        "--capacity-mib", str(profile["local_memory_capacity_mib"]),
        "--numa-node", str(args.local_node),
        *(["--numa-nodes", ",".join(str(n["numa_node"]) for n in domains),
           "--cpus", ",".join(n["cpus"] for n in domains)] if domains else []),
        "--shared-borrow-mib", str(profile["shared_memory_borrow_limit_mib"] or 0),
        *(["--shared-node", str(profile["warm_numa_node"])] if args.warm else []), timeout=120,
    )
    command(
        "sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}",
        str(Path(profile["local_memory_cgroup"])/"memory.reclaim"),
    )
    if args.warm:
        root = Path(args.warm_root)
        if not root.is_absolute() or root == Path("/"):
            raise ValueError("WARM root must be an absolute dedicated directory")
        env = command("systemctl", "show", "cube-sandbox-cubelet", "--property=Environment", "--value")
        required = (f"CLAWBOX_WARM_SNAPSHOT_ROOT={root}", "CLAWBOX_KVM_DIRTY_TRACKING=1", "CUBE_RESTORE_PRIVATE_COPY=0")
        if any(value not in env.split() for value in required):
            raise ValueError("Cubelet WARM settings do not match; configure the patched service using docs/installation.md")
        command("sudo", "-n", "mkdir", "-p", str(root))
        mounted = subprocess.run(["findmnt", "-M", str(root)], capture_output=True).returncode == 0
        if not mounted:
            if any(root.iterdir()):
                raise ValueError(f"Refusing to hide existing files under WARM root: {root}")
            command("sudo", "-n", "mount", "-t", "tmpfs", "-o",
                    f"size={args.warm_gib}G,mode=0770,uid={os.getuid()},gid={os.getgid()},mpol=bind:{args.warm_node}",
                    "clawbox-warm", str(root))
        if command("findmnt", "-n", "-o", "FSTYPE", "-M", str(root)) != "tmpfs":
            raise ValueError("WARM directory is not tmpfs; refusing disk-backed snapshots")
        if shutil.disk_usage(root).total != args.warm_gib * 1024**3:
            if shutil.disk_usage(root).used > args.warm_gib * 1024**3:
                raise ValueError("Requested WARM capacity is smaller than existing snapshot data")
            # Resize an idle pool without hiding or removing its contents.
            command("sudo", "-n", "mount", "-o", f"remount,size={args.warm_gib}G", str(root))
            if shutil.disk_usage(root).total != args.warm_gib * 1024**3:
                raise RuntimeError("WARM tmpfs resize did not reach the requested capacity")
        if service_setting("cube-sandbox-cubemaster", "CLAWBOX_WARM_SNAPSHOT_ROOT") != str(root):
            # The standalone launcher sources this file itself, so a systemd
            # Environment override would be overwritten by its shell script.
            command("sudo", "-n", sys.executable, "-c", r"""
import pathlib, re, shlex, shutil, sys, time
p = pathlib.Path('/usr/local/services/cubetoolbox/.one-click.env')
text = p.read_text()
shutil.copy2(p, p.with_name(p.name + '.pre-lab-' + str(time.time_ns())))
key = 'CLAWBOX_WARM_SNAPSHOT_ROOT'
text = re.sub(r'^(?:export )?' + key + r'=.*\n?', '', text, flags=re.M)
p.write_text(text.rstrip() + '\n' + key + '=' + shlex.quote(sys.argv[1]) + '\n')
""", str(root))
            command("sudo", "-n", "systemctl", "restart", "cube-sandbox-cubemaster", timeout=120)
        if service_setting("cube-sandbox-cubelet", "CLAWBOX_WARM_SNAPSHOT_ROOT") != str(root):
            command("sudo", "-n", "systemctl", "restart", "cube-sandbox-cubelet", timeout=120)
    if args.warm:
        wait_for_snapshot_storage(profile)
    if doctor(profile, warm=args.warm):
        raise RuntimeError("Host checks failed; profile not saved")
    wait_for_vm_ready(profile)
    profile['tcp_tw_reuse'] = getattr(args, 'tcp_tw_reuse', None)
    profile['ip_local_port_range'] = getattr(args, 'ip_local_port_range', None)
    profile['cube_proxy_connect_timeout_seconds'] = getattr(
        args, 'cube_proxy_connect_timeout_seconds', None
    )
    profile['cubelet_tap_init_num'] = getattr(args, 'cubelet_tap_init_num', None)
    profile['cube_scheduler_overcommit'] = getattr(args, 'cube_scheduler_overcommit', None)
    profile['cube_scheduler_vm_count_ratio'] = getattr(args, 'cube_scheduler_vm_count_ratio', None)
    profile['cube_master_log_level'] = getattr(args, 'cube_master_log_level', None)
    profile['cube_master_db_timeout_seconds'] = getattr(
        args, 'cube_master_db_timeout_seconds', None
    )
    _write_json_atomic(args.profile, profile)
    print(f"Saved host profile: {args.profile}")
    return profile

