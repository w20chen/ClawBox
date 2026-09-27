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

import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("cube-sandbox-cubeops", "cube-sandbox-cubemaster",
            "cube-sandbox-cube-api", "cube-sandbox-cubelet")
CONTAINERS = ("cube-sandbox-mysql", "cube-sandbox-redis", "cube-sandbox-minio",
              "cube-proxy-coredns", "cube-proxy", "cube-egress", "cube-lifecycle-manager")


def command(*args: str, timeout: int = 30) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Command failed: {' '.join(args)} (exit {result.returncode})")
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


def wait_for_vm_ready(profile: dict) -> None:
    from cubesandbox import Sandbox, ApiError
    deadline = time.monotonic() + 600
    print("Checking VM creation and guest execution (node startup can take several minutes)...", flush=True)
    while True:
        cli = Path("/usr/local/services/cubetoolbox/CubeMaster/bin/cubemastercli")
        if cli.is_file():
            try:
                command(str(cli), "--address", "127.0.0.1", "--timeout", "10s",
                        "storage", "status", "--refresh", "--json", timeout=15)
            except (RuntimeError, subprocess.TimeoutExpired):
                pass  # Node boot may still be in progress; create proves readiness.
        try:
            probe = Sandbox.create(template=profile["sandbox"]["template_id"], timeout=120,
                                   lifecycle={"on_timeout": "kill", "auto_resume": False},
                                   distribution_scope=[profile["node"]],
                                   metadata={"clawbox.owner": "lab-setup-" + uuid.uuid4().hex})
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
    for module in ("jsonschema", "cryptography", "yaml", "httpx"):
        checks.append(("Python " + module, lambda m=module: bool(importlib.import_module(m))))
    for unit in SERVICES:
        checks.append((unit, lambda u=unit: command("systemctl", "is-active", u) == "active"))
    for name in CONTAINERS:
        checks.append((name, lambda n=name: command("docker", "inspect", "--format", "{{.State.Running}}", n) == "true"))
    for role in ("runtime", "sandbox"):
        checks.append((role + " template", lambda r=role: bool(template_record(profile[r]["template_id"]))))
    if current_images:
        from .clawtune_integration import source_revision
        revision = source_revision(Path(os.environ["CLAWTUNE_ROOT"]))
        def matches(role):
            item = sdk()[1].get(profile[role]["template_id"])
            containers = (item.create_request or {}).get("containers") or []
            env = {e["key"]: e.get("value") for c in containers for e in c.get("envs", [])}
            return revision != "unknown" and env.get("CLAWTUNE_REVISION") == revision
        for role in ("runtime", "sandbox"):
            checks.append((role + " current ClawTune (update with lab images)", lambda r=role: matches(r)))
    if warm:
        for unit in ("cube-sandbox-cubemaster", "cube-sandbox-cubelet"):
            checks.append((unit + " WARM root", lambda u=unit: service_setting(u, "CLAWBOX_WARM_SNAPSHOT_ROOT") == profile["warm_root"]))
        for key, value in (("CLAWBOX_KVM_DIRTY_TRACKING", "1"), ("CUBE_RESTORE_PRIVATE_COPY", "0")):
            checks.append((key, lambda k=key, v=value: service_setting("cube-sandbox-cubelet", k) == v))
        checks.append(("WARM is tmpfs", lambda: command("findmnt", "-n", "-o", "FSTYPE", "-M", profile["warm_root"]) == "tmpfs"))
        checks.append(("incremental SDK", lambda: "snapshot_mechanism" in inspect.signature(sdk()[0].pause).parameters))
        checks.append(("host swap disabled", lambda: len(Path("/proc/swaps").read_text().splitlines()) == 1))
    for name, check in checks:
        try:
            ok = bool(check())
        except Exception:
            ok = False
        print(f"{'OK' if ok else 'FAIL'}  {name}", flush=True)
        if not ok:
            failures.append(name)
    return failures


def setup(args) -> dict:
    if not Path("/dev/kvm").exists():
        raise ValueError("Run setup on the Linux KVM host, not the Windows client")
    if shutil.disk_usage(ROOT).free < 5 * 1024**3:
        raise ValueError("Less than 5 GiB free. Free disk space before starting services; setup never deletes old data")
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
    previous = json.loads(args.profile.read_text()) if args.profile.exists() else {}
    runtime = args.runtime_template or previous.get("runtime", {}).get("template_id") or os.getenv("CLAWBOX_RUNTIME_TEMPLATE")
    tool = args.tool_template or previous.get("sandbox", {}).get("template_id") or os.getenv("CLAWBOX_TOOL_TEMPLATE")
    if not runtime or not tool:
        raise ValueError("Supply --runtime-template and --tool-template, or set them in machine.env")
    profile = {"runtime": template_record(runtime), "sandbox": template_record(tool)}
    nodes = set(profile["runtime"].pop("nodes")) & set(profile["sandbox"].pop("nodes"))
    node = args.node or previous.get("node") or os.getenv("CUBE_NODE")
    if node is None and len(nodes) == 1:
        node = nodes.pop()
        nodes.add(node)
    if node not in nodes:
        raise ValueError("Select a node with READY replicas of both templates using --node")
    profile.update(node=node, warm_root=args.warm_root, warm_capacity_mib=args.warm_gib * 1024)
    apply_environment(profile)
    if args.warm:
        if any(x.get("state") == "running" for x in sdk()[0].list_v2()):
            raise ValueError("WARM setup requires an idle VM host; existing VMs will not be stopped")
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
            command("sudo", "-n", "mount", "-t", "tmpfs", "-o",
                    f"size={args.warm_gib}G,mode=0770,uid={os.getuid()},gid={os.getgid()},mpol=bind:{args.warm_node}",
                    "clawbox-warm", str(root))
        if command("findmnt", "-n", "-o", "FSTYPE", "-M", str(root)) != "tmpfs":
            raise ValueError("WARM directory is not tmpfs; refusing disk-backed snapshots")
        if shutil.disk_usage(root).total < args.warm_gib * 1024**3:
            raise ValueError("Existing WARM mount is smaller than requested; refusing to remount it implicitly")
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
    if doctor(profile, warm=args.warm):
        raise RuntimeError("Host checks failed; profile not saved")
    wait_for_vm_ready(profile)
    args.profile.parent.mkdir(parents=True, exist_ok=True)
    args.profile.write_text(json.dumps(profile, indent=2) + "\n")
    print(f"Saved host profile: {args.profile}")
    return profile


def prepare_spec(args, profile: dict):
    from .experiments.spec import ExperimentSpec
    from .experiments.baselines import BASELINES
    from .experiments.preset_view import select_presets
    raw = yaml.safe_load(args.spec.read_text())
    names = select_presets(args.baseline or (), **{key: getattr(args, key) for key in
                            ("reserve_during", "estimate", "idle", "resume")})
    if not names:
        names = ("tool-full-resident",)
    raw["policies"] = [BASELINES[n].as_policy().model_dump(mode="json") for n in names]
    raw.setdefault("execution", {})["concurrency_levels"] = args.concurrency
    raw["execution"]["randomized_order"] = False
    for role in ("runtime", "sandbox"):
        raw.setdefault(role, {}).pop("template_alias", None)
        raw[role].update(profile[role])
    resources = raw.setdefault("resources", {})
    resources.update(target_node=profile["node"], full_tool_memory_mib=profile["sandbox"]["memory_mib"])
    if args.pool_gib:
        resources["pool_memory_budget_mib"] = args.pool_gib * 1024
    resources.update(snapshot_storage="warm-only" if args.storage == "memory" else "tiered",
                     warm_snapshot_root=profile["warm_root"], warm_memory_capacity_mib=profile["warm_capacity_mib"])
    if args.storage == "memory":
        resources["cold_snapshot_root"] = None
    # Paths are resolved from the caller's repository, as in clawbox experiment.
    if args.trace:
        cases = raw["workload"].get("cases") or []
        if len(cases) != 1:
            raise ValueError("--trace requires a single-case spec; multi-case traces belong in YAML")
        cases[0].update(source_reference=str(args.trace.resolve()), replay_trace_reference=str(args.trace.resolve()))
    raw.setdefault("inference", {})["backend"] = "replay"
    if getattr(args, "model", None):
        raw["inference"].setdefault("configuration", {})["model"] = args.model
    if getattr(args, "max_model_steps", None):
        raw["inference"].setdefault("configuration", {})["max_model_steps"] = args.max_model_steps
    return ExperimentSpec.model_validate(raw)


def cleanup(state: dict, directory: Path) -> None:
    from .cube.client import CubeSandboxClient, OwnedSandboxJournal
    journal = OwnedSandboxJournal(directory/"owned-sandboxes.jsonl")
    CubeSandboxClient(journal=journal).kill_owned_sandboxes(state["owner_id"])
    cleanup_warm_snapshots(Path(state["profile"]["warm_root"]), journal.sandbox_ids(task_uid=state["owner_id"]))
    print("Cleanup verified: no VMs or WARM snapshots owned by this run remain", flush=True)


def cleanup_warm_snapshots(root: Path, sandbox_ids: list[str]) -> None:
    # A failed generation can leave files absent from Cube's committed snapshot
    # metadata. Only remove journal-owned directories after VM deletion succeeds.
    if not root.exists():
        return
    if not root.is_absolute() or root == Path("/") or root.is_symlink():
        raise ValueError("Unsafe WARM cleanup root")
    targets = []
    for sandbox_id in sandbox_ids:
        if not re.fullmatch(r"[0-9a-f]{32}", sandbox_id):
            raise ValueError("Invalid sandbox ID for WARM cleanup")
        target = root/sandbox_id
        if target.is_symlink() or target.resolve().parent != root.resolve():
            raise ValueError("WARM cleanup target escapes root")
        if target.exists():
            targets.append(target)
    if not targets:
        return
    if command("findmnt", "-n", "-o", "FSTYPE", "-M", str(root)) != "tmpfs":
        raise ValueError("WARM cleanup requires the configured tmpfs mount")
    for target in targets:
        command("sudo", "-n", "rm", "-rf", "--", str(target), timeout=120)
        if target.exists():
            raise RuntimeError(f"WARM cleanup incomplete: {target}")


def run(args, profile: dict) -> int:
    from .experiments.inputs import validate_inputs
    from .experiments.worker import ExperimentWorker
    spec = prepare_spec(args, profile)
    validate_inputs(spec)
    from .experiments.llm_config import resolve_llm_configuration
    configuration, _ = resolve_llm_configuration(spec.inference.configuration, live=False)
    if not configuration.get("model"):
        raise ValueError("Specify the recorded model with --model or inference.configuration.model before starting VMs")
    warm = any(p.reclamation.value != "resident" for p in spec.policies)
    if doctor(profile, warm=warm and args.storage == "memory", current_images=True):
        raise RuntimeError("Preflight failed; no VMs started. Run scripts/lab setup first")
    run_id = args.run_id or time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", run_id):
        raise ValueError("Invalid run ID")
    output = args.output_root / run_id
    output.mkdir(parents=True, exist_ok=False)
    state = {"run_id": run_id, "owner_id": "lab-" + uuid.uuid4().hex,
             "attempt_id": uuid.uuid4().hex, "profile": profile}
    (output / "lab-state.json").write_text(json.dumps(state, indent=2) + "\n")
    (output / "experiment.yaml").write_text(yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False))
    print(f"Run: {run_id}\nResults: {output}\nConcurrency: {args.concurrency} (two VMs per session)", flush=True)
    print("Policies: " + ", ".join(p.name for p in spec.policies), flush=True)
    old = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        results = ExperimentWorker(spec, run_id=run_id, attempt_id=state["attempt_id"],
                                   task_uid=state["owner_id"], output_root=output).run()
        for result in results:
            print(f"{result.arm.policy.name} c{result.arm.concurrency}: {result.status.value}", flush=True)
            if result.correctness.get("failure"):
                print("  " + str(result.correctness["failure"]), flush=True)
        return 0 if results and all(r.status.value == "succeeded" for r in results) else 1
    finally:
        signal.signal(signal.SIGTERM, old)
        cleanup(state, output)
        print(f"Report: {output / 'summary.md'}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Start, run and clean up standalone CubeSandbox experiments")
    parser.add_argument("--profile", type=Path, default=Path.home()/".config/clawbox/lab.json")
    parser.add_argument("--output-root", type=Path, default=Path.home()/"clawbox-results")
    sub = parser.add_subparsers(dest="action", required=True)
    setup_parser = sub.add_parser("setup", help="Start installed services and save verified template settings")
    setup_parser.add_argument("--runtime-template")
    setup_parser.add_argument("--tool-template")
    setup_parser.add_argument("--node")
    setup_parser.add_argument("--warm", action="store_true", help="Prepare optional tmpfs snapshot storage on an idle host")
    setup_parser.add_argument("--warm-root", default="/mnt/clawbox-warm")
    setup_parser.add_argument("--warm-gib", type=int, default=64)
    setup_parser.add_argument("--warm-node", type=int, default=1)
    sub.add_parser("doctor", help="Check services, disk, KVM and templates")
    sub.add_parser("baselines", help="List supported policies and their dimensions")
    repair = sub.add_parser("repair-warm", help="Patch, test and rebuild Cubelet's interrupted WARM allocation handling on an idle host")
    repair.add_argument("--source", default=os.getenv("CUBE_SOURCE_DIR"))
    repair.add_argument("--go", default=os.getenv("CLAWBOX_GO", "go"))
    repair.add_argument("--cow-sdk", default=os.getenv("CLAWBOX_COW_SDK"))
    images = sub.add_parser("images", help="Rebuild current integrations on the selected task images and register new templates")
    images.add_argument("--registry", default="127.0.0.1:5000/clawbox")
    images.add_argument("--go", default=os.getenv("CLAWBOX_GO", "go"))
    images.add_argument("--pip-index", default="https://pypi.org/simple")
    images.add_argument("--direct-network", action="store_true", help="Do not use inherited HTTP/SOCKS proxies for builds")
    images.add_argument("--role", choices=["runtime", "sandbox"], action="append")
    images.add_argument("--kernel-source", default=os.getenv("CLAWBOX_GUEST_KERNEL_SOURCE"))
    images.add_argument("--kernel-build", default=os.getenv("CLAWBOX_GUEST_KERNEL_BUILD"))
    runner = sub.add_parser("run", help="Run trace(s), collect results, and always destroy this run's VMs")
    runner.add_argument("spec", type=Path, help="Task YAML with prompt, trace and validation command")
    runner.add_argument("--trace", type=Path)
    runner.add_argument("--model", help="Recorded model name (no API key is needed for replay)")
    runner.add_argument("--max-model-steps", type=int, help="Replay a prefix; result is not full-task completion")
    runner.add_argument("--concurrency", type=int, nargs="+", default=[1])
    runner.add_argument("--baseline", action="append")
    from .experiments.preset_view import DIMENSIONS
    for key, values in DIMENSIONS.items():
        runner.add_argument("--"+key.replace("_", "-"), choices=values, action="append")
    runner.add_argument("--storage", choices=["memory", "disk"], default="memory",
                        help="memory forbids all COLD fallbacks; disk explicitly permits configured tiered storage")
    runner.add_argument("--pool-gib", type=int)
    runner.add_argument("--run-id")
    for action in ("status", "cleanup"):
        sub.add_parser(action).add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "baselines":
            from .cli import main as cli
            result = cli(["experiment", "baselines"])
            print("Predicted admission requires available LatticeKB memory_extra_peak_bytes; missing evidence fails without fallback.")
            return result
        if args.action == "setup":
            setup(args)
            return 0
        if args.action == "images":
            return subprocess.call([sys.executable, str(ROOT/"scripts/refresh-lab-images.py"),
                                    "--profile", str(args.profile), "--registry", args.registry,
                                    "--go", args.go, "--pip-index", args.pip_index,
                                    *[value for role in args.role or [] for value in ("--role", role)],
                                    *(["--kernel-source", args.kernel_source] if args.kernel_source else []),
                                    *(["--kernel-build", args.kernel_build] if args.kernel_build else []),
                                    *(["--direct-network"] if args.direct_network else [])])
        if args.action == "repair-warm":
            return subprocess.call([sys.executable, str(ROOT/"scripts/repair-cube-warm.py"),
                                    "--go", args.go, *(["--source", args.source] if args.source else []),
                                    *(["--cow-sdk", args.cow_sdk] if args.cow_sdk else [])])
        if args.action in {"status", "cleanup"}:
            state = json.loads((args.directory/"lab-state.json").read_text())
            apply_environment(state["profile"])
            if args.action == "cleanup":
                cleanup(state, args.directory)
            else:
                report = args.directory/"summary.md"
                if report.exists():
                    print(report.read_text())
                else:
                    print("No final report yet (this does not prove the worker is still running).")
                    for path in sorted((args.directory/"events").glob("*.jsonl")):
                        rows = path.read_text().splitlines()
                        if rows:
                            try:
                                event = json.loads(rows[-1])
                                print(path.stem, event.get("event"), event.get("wall_time"))
                            except json.JSONDecodeError:
                                pass
            return 0
        profile = json.loads(args.profile.read_text())
        apply_environment(profile)
        if args.action == "doctor":
            return 2 if doctor(profile) else 0
        return run(args, profile)
    except KeyboardInterrupt:
        print("Interrupted. Cleanup was requested; use cleanup DIRECTORY if the host was unreachable.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
