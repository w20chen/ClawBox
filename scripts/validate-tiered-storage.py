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
from clawbox.experiments.memory import SandboxRSSSampler

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--template", required=True)
parser.add_argument("--node", required=True)
parser.add_argument("--warm", required=True, type=Path)
parser.add_argument("--cold", required=True, type=Path)
parser.add_argument("--output", required=True, type=Path)
parser.add_argument("--helper-image", required=True)
parser.add_argument("--require-local-numa", type=int)
parser.add_argument("--expected-memory-mib", type=int, default=4096)
parser.add_argument("--snapshot-mechanism", choices=("full-copy", "incremental-cow"),
                    default="full-copy")
parser.add_argument("--rounds", type=int, default=2)
parser.add_argument("--ready-timeout", type=float, default=0,
                    help="wait only for the explicit no-ready-template-replica precondition")
args = parser.parse_args()
if args.rounds < 2:
    parser.error("--rounds must be at least 2 (the last generation spills to COLD)")
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
        report.setdefault("local_charge_bytes", {})[label] = int(local_stat.get("anon", "0"))
        if label == "initial-local" and int(local_stat.get("anon", "0")) < 8 * 1024 * 1024:
            raise RuntimeError("LOCAL cgroup has no resident Guest working set")
        for item in vmm:
            allowed = re.search(r"^Mems_allowed_list:\s*(.+)$", item["status"], re.M)
            if not allowed or allowed.group(1).strip() != str(node):
                raise RuntimeError("VMM is not restricted to the LOCAL NUMA node")
            if label != "initial-local":
                # A lazy restore intentionally retains file-backed mappings;
                # charging every Guest page to LOCAL before the first tool
                # would silently turn this gate back into eager restore.
                continue
            # With lazy restore, Guest RAM may remain file-backed until the
            # first access. Verify the VMM's allowed NUMA node and absence of
            # stale tier mappings; do not require an eagerly materialized anon
            # mapping here.
            for line in item["numa_maps"].splitlines():
                if any(int(n) != node and int(pages) > 0
                       for n, pages in re.findall(r"\bN(\d+)=(\d+)", line)):
                    raise RuntimeError("guest RAM has physical pages outside LOCAL NUMA")


def probe(expected_round):
    result = vm.commands.run("curl -fsS http://127.0.0.1:18099/", timeout=30)
    if result.exit_code or json.loads(result.stdout) != {
        "token": owner, "sum": 81 * 16 * 1024 * 1024 + expected_round,
        "round": expected_round,
    }:
        raise RuntimeError("restored process memory/token check failed: " + result.stdout + result.stderr)


def mutate(round_number):
    result = vm.commands.run(
        f"curl -fsS 'http://127.0.0.1:18099/write?round={round_number}'", timeout=30)
    if result.exit_code or json.loads(result.stdout).get("round") != round_number:
        raise RuntimeError("Guest process state mutation failed: " + result.stdout + result.stderr)


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
              "from urllib.parse import urlsplit,parse_qs\n"
              "data=bytearray(b'Q'*(16*1024*1024))\n"
              "round_number=0\n"
              "class Handler(BaseHTTPRequestHandler):\n"
              " def do_GET(self):\n"
              "  global round_number\n"
              "  url=urlsplit(self.path)\n"
              "  if url.path=='/write':\n"
              "   next_round=int(parse_qs(url.query)['round'][0])\n"
              "   if next_round!=round_number+1: self.send_error(409); return\n"
              "   data[0]=81+next_round; round_number=next_round\n"
              f"  body=json.dumps({{'token':{owner!r},'sum':sum(data),'round':round_number}}).encode()\n"
              "  self.send_response(200); self.end_headers(); self.wfile.write(body)\n"
              "HTTPServer(('127.0.0.1',18099),Handler).serve_forever()\n")
    vm.files.write("/tmp/tier-state.py", source)
    result = vm.commands.run("nohup python3 /tmp/tier-state.py >/tmp/tier-state.log 2>&1 </dev/null &", timeout=30)
    if result.exit_code:
        raise RuntimeError(result.stderr)
    for attempt in range(30):
        try:
            probe(0)
            break
        except Exception:
            if attempt == 29:
                raise
            time.sleep(0.2)
    evidence("initial-local")
    for generation in range(1, args.rounds + 1):
        spill = generation == args.rounds
        mutate(generation)
        warm = args.warm / vm.sandbox_id / f"gate-g{generation}.mem"
        warm.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        manifest = CubeSandboxClient.pause_sandbox(
            vm, tier="warm", memory_snapshot_path=str(warm), generation=generation,
            snapshot_mechanism=args.snapshot_mechanism)
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
        vm = Sandbox.connect(
            vm.sandbox_id,
            **({"snapshot_mechanism": "incremental-cow"}
               if args.snapshot_mechanism == "incremental-cow" else {}))
        restore_ready = time.monotonic() - started
        sampler = SandboxRSSSampler(vm.sandbox_id)
        sampler.start()
        first_tool_started = time.monotonic()
        probe(generation)
        first_tool_seconds = time.monotonic() - first_tool_started
        report["checks"].append({
            "phase": "restore-cold" if spill else "restore-warm",
            "restore_ready_seconds": restore_ready,
            "first_tool_after_restore_seconds": first_tool_seconds,
            "first_tool_host_cost": sampler.stop(),
        })
        evidence("restored", backing)
    def percentile(values, q):
        values = sorted(values)
        index = (len(values) - 1) * q
        lower = int(index)
        return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower)
    checkpoints = [item for item in report["checks"] if item["phase"] == "checkpoint-warm"]
    steady = checkpoints[1:]
    restores = [item for item in report["checks"] if item["phase"].startswith("restore-")]
    report["summary"] = {
        "snapshot_mechanism": args.snapshot_mechanism,
        "rounds": args.rounds,
        "checkpoint_p50_seconds": percentile([item["seconds"] for item in checkpoints], .5),
        "checkpoint_p95_seconds": percentile([item["seconds"] for item in checkpoints], .95),
        "steady_checkpoint_p50_seconds": percentile([item["seconds"] for item in steady], .5),
        "steady_checkpoint_p95_seconds": percentile([item["seconds"] for item in steady], .95),
        "restore_ready_p50_seconds": percentile([item["restore_ready_seconds"] for item in restores], .5),
        "restore_ready_p95_seconds": percentile([item["restore_ready_seconds"] for item in restores], .95),
        "first_tool_p50_seconds": percentile([item["first_tool_after_restore_seconds"] for item in restores], .5),
        "first_tool_p95_seconds": percentile([item["first_tool_after_restore_seconds"] for item in restores], .95),
    }
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
