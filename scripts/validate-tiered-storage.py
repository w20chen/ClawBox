#!/usr/bin/env python3
"""Exercise direct WARM restore and WARM-to-COLD spill with process-state checks.

This storage gate does not certify physical LOCAL enforcement. It records host
NUMA/mapping evidence for inspection, using an explicit privileged helper image.
"""
import argparse
import json
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from cubesandbox import Sandbox
from clawbox.cube import CubeSandboxClient

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--template", required=True)
parser.add_argument("--node", required=True)
parser.add_argument("--warm", required=True, type=Path)
parser.add_argument("--cold", required=True, type=Path)
parser.add_argument("--output", required=True, type=Path)
parser.add_argument("--helper-image", required=True)
parser.add_argument("--require-local-numa", type=int)
parser.add_argument("--expected-memory-mib", type=int, default=4096)
parser.add_argument("--ready-timeout", type=float, default=0,
                    help="wait only for the explicit no-ready-template-replica precondition")
args = parser.parse_args()
owner = "tier-gate-" + uuid.uuid4().hex[:12]
report = {"owner": owner, "template": args.template, "checks": [], "passed": False}
vm = None


def evidence(label, snapshot=None):
    command = ["docker", "run", "--rm", "--privileged", "--pid=host", "-v", "/:/host",
               "--entrypoint", "chroot", args.helper_image, "/host", "python3",
               str(Path(__file__).with_name("inspect-tier-pages.py").resolve()),
               "--sandbox-id", vm.sandbox_id]
    if snapshot:
        command += ["--snapshot", str(snapshot)]
    result = json.loads(subprocess.check_output(command, text=True, timeout=90))
    report["checks"].append({"phase": label, "evidence": result})
    if args.require_local_numa is not None and label in {"initial-local", "restored"}:
        vmm = [item for item in result["processes"]
               if "containerd-shim-cube-rs" in item["command"]]
        if not vmm:
            raise RuntimeError("no VMM found for physical LOCAL verification")
        node = args.require_local_numa
        local_stat = dict(line.split() for line in
                          result["cgroups"]["local"]["memory.stat"].splitlines())
        if int(local_stat.get("anon", "0")) < args.expected_memory_mib * 1024 * 1024:
            raise RuntimeError("guest RAM is not fully charged to the LOCAL cgroup")
        for item in vmm:
            allowed = re.search(r"^Mems_allowed_list:\s*(.+)$", item["status"], re.M)
            if not allowed or allowed.group(1).strip() != str(node):
                raise RuntimeError("VMM is not restricted to the LOCAL NUMA node")
            if str(args.warm) in item["numa_maps"] or str(args.cold) in item["numa_maps"]:
                raise RuntimeError("restored VMM still retains a tier snapshot mapping")
            large_anon = [line for line in item["numa_maps"].splitlines()
                          if "file=" not in line and
                          int((re.search(r"\banon=(\d+)", line) or [None, "0"])[1]) > 4096]
            if not large_anon:
                raise RuntimeError("no independently allocated guest RAM mapping found")
            for line in large_anon:
                if any(int(n) != node and int(pages) > 0
                       for n, pages in re.findall(r"\bN(\d+)=(\d+)", line)):
                    raise RuntimeError("guest RAM has physical pages outside LOCAL NUMA")


def probe():
    result = vm.commands.run("curl -fsS http://127.0.0.1:18099/", timeout=30)
    if result.exit_code or json.loads(result.stdout) != {"token": owner, "sum": 81 * 16 * 1024 * 1024}:
        raise RuntimeError("restored process memory/token check failed: " + result.stdout + result.stderr)


try:
    ready_started = time.monotonic()
    while True:
        try:
            vm = Sandbox.create(template=args.template, timeout=600,
                                metadata={"clawbox.owner": owner}, distribution_scope=[args.node])
            break
        except Exception as exc:
            if ("template has no ready replica" not in str(exc) or
                    time.monotonic() - ready_started >= args.ready_timeout):
                raise
            print("waiting for Cubelet template replica readiness", flush=True)
            time.sleep(5)
    report["ready_and_create_seconds"] = time.monotonic() - ready_started
    report["sandbox_id"] = vm.sandbox_id
    source = ("from http.server import BaseHTTPRequestHandler,HTTPServer\nimport json\n"
              "data=bytearray(b'Q'*(16*1024*1024))\n"
              "class Handler(BaseHTTPRequestHandler):\n"
              " def do_GET(self):\n"
              f"  body=json.dumps({{'token':{owner!r},'sum':sum(data)}}).encode()\n"
              "  self.send_response(200); self.end_headers(); self.wfile.write(body)\n"
              "HTTPServer(('127.0.0.1',18099),Handler).serve_forever()\n")
    vm.files.write("/tmp/tier-state.py", source)
    result = vm.commands.run("nohup python3 /tmp/tier-state.py >/tmp/tier-state.log 2>&1 </dev/null &", timeout=30)
    if result.exit_code:
        raise RuntimeError(result.stderr)
    for attempt in range(30):
        try:
            probe()
            break
        except Exception:
            if attempt == 29:
                raise
            time.sleep(0.2)
    evidence("initial-local")
    for generation, spill in [(1, False), (2, True)]:
        warm = args.warm / vm.sandbox_id / f"gate-g{generation}.mem"
        warm.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        manifest = CubeSandboxClient.pause_sandbox(vm, tier="warm", memory_snapshot_path=str(warm), generation=generation)
        report["checks"].append({"phase": "checkpoint-warm", "manifest": manifest, "seconds": time.monotonic()-started})
        evidence("paused-warm", warm)
        backing = warm
        if spill:
            cold = args.cold / vm.sandbox_id / f"gate-g{generation}.mem"
            cold.parent.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            CubeSandboxClient.relocate_snapshot(vm, tier="cold", memory_snapshot_path=str(cold), generation=generation)
            report["checks"].append({"phase": "spill-cold", "seconds": time.monotonic()-started, "warm_exists": warm.exists()})
            if warm.exists():
                raise RuntimeError("WARM source remains after spill")
            evidence("paused-cold", cold)
            if args.require_local_numa is not None:
                cold_pages = report["checks"][-1]["evidence"]["snapshot"]["resident_pages_before_probe"]
                if cold_pages:
                    raise RuntimeError(f"COLD snapshot retains {cold_pages} cached pages")
            backing = cold
        started = time.monotonic()
        vm = Sandbox.connect(vm.sandbox_id)
        probe()
        report["checks"].append({"phase": "restore-cold" if spill else "restore-warm", "seconds": time.monotonic()-started})
        evidence("restored", backing)
    report["passed"] = True
except Exception as exc:
    report["error"] = f"{type(exc).__name__}: {exc}"
    raise
finally:
    if vm is not None:
        try:
            vm.kill()
            report["cleanup"] = "killed-owned-sandbox"
        except Exception as exc:
            report["cleanup_error"] = str(exc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "checks"}))
