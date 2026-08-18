#!/usr/bin/env python3
"""CBX-M0-005: release/evidence manifest generator.

Collects a re-verifiable evidence manifest for a ClawBox release / cluster /
run WITHOUT reading any credential plaintext (Secret names only, never data).

Output layout (see roadmap §16.1):

    release-evidence/<release>/<cluster>/<run-id>/
      manifest.json        every fact + file SHA256; self-hashed
      gate-summary.json    per-gate pass/fail/blocked + command + timestamps
      e2e/                 rendered CR / Pod / Job / NetworkPolicy YAML digests

Usage (run on the target machine, or anywhere with kubectl/docker + the repo):

    python3 scripts/evidence-manifest.py collect \
        --release m0-2026-08-18 --cluster hostname --run <run-id> \
        [--namespace clawbox-benchmarks] [--gate e2e-real-task:pass] ...

    python3 scripts/evidence-manifest.py verify --manifest <path>/manifest.json

`verify` recomputes every recorded digest and fact hash from the evidence dir
and fails on any mismatch, so the same evidence can be re-validated later.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[1]


def sh(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a command; never raises. Returns (returncode, combined output)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return -1, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, f"timeout after {timeout}s: {' '.join(cmd)}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_facts() -> dict[str, object]:
    facts: dict[str, object] = {}
    code, out = sh(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    facts["sha"] = out.strip() if code == 0 else None
    code, out = sh(["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"])
    facts["branch"] = out.strip() if code == 0 else None
    code, out = sh(["git", "-C", str(ROOT), "status", "--porcelain"])
    facts["dirty"] = bool(out.strip()) if code == 0 else None
    if facts["dirty"]:
        facts["dirty_files"] = sorted(line[:2] + " " + line[3:] for line in out.splitlines() if line)
    return facts


def image_digest(ref: str) -> str | None:
    """Resolve the digest of a locally-present image via docker (no pull)."""
    code, out = sh(["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", ref])
    if code == 0 and "@" in out:
        return out.strip().split("@", 1)[1]
    code, out = sh(["docker", "image", "inspect", "--format", "{{.Id}}", ref])
    if code == 0 and out.startswith("sha256:"):
        return out.strip()
    return None


def first_line(out: str) -> str | None:
    line = out.strip().splitlines()[0] if out.strip() else ""
    return line or None


def host_facts() -> dict[str, object]:
    facts: dict[str, object] = {}
    facts["arch"] = first_line(sh(["uname", "-m"])[1])
    facts["kernel"] = first_line(sh(["uname", "-r"])[1])
    for name, cmd in (
        ("kubelet", ["kubelet", "--version"]),
        ("containerd", ["containerd", "--version"]),
        ("ctr", ["ctr", "version"]),
        ("kata", ["kata-runtime", "--version"]),
        ("firecracker", ["firecracker", "--version"]),
    ):
        code, out = sh(cmd)
        facts[name] = first_line(out) if code == 0 else None
    code, out = sh(["kubectl", "version", "--output=json"], timeout=15)
    if code == 0:
        try:
            ver = json.loads(out)
            server = ver.get("serverVersion", {})
            facts["k8s_server"] = f"{server.get('major')}.{server.get('minor')}"
        except json.JSONDecodeError:
            facts["k8s_server"] = None
    code, out = sh(["kubectl", "get", "nodes", "-o", "jsonpath={.items[0].metadata.name}"], timeout=15)
    facts["node"] = out.strip() if code == 0 else None
    return facts


def schema_facts() -> dict[str, object]:
    """Version/schema constants extracted from the repo (no credentials)."""
    facts: dict[str, object] = {}

    def grep(pattern: str, *paths: str) -> str | None:
        for rel in paths:
            path = ROOT / rel
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                match = re.search(pattern, line)
                if match:
                    return match.group(1) or match.group(0).strip()
        return None

    crd = ROOT / "deploy" / "sandboxtask-crd.yaml"
    facts["crd_versions"] = grep(r"^\s*- name: (v1[a-z0-9]*)", "deploy/sandboxtask-crd.yaml")
    facts["crd_file_sha256"] = sha256_file(crd)
    facts["runtimeclass"] = grep(r"name: (kata-fc-arm64)", "deploy/runtimeclass-firecracker.yaml")
    facts["observation_schema"] = grep(r'"schema_version":\s*"?(\d+)', "scripts/runtime-entrypoint.sh")
    facts["protocol_version"] = grep(r"schema[_-]?version[^\n]*[:=]\s*[\"']?(\d+)", "toolbridge/main.go")
    facts["state_schema_version"] = grep(r'STATE_SCHEMA_VERSION="(\d+)"', "scripts/bootstrap-openeuler-arm64.sh")
    return facts


def kubectl_facts(namespace: str, run_id: str) -> dict[str, object]:
    """Per-run Kubernetes facts: terminal phase and rendered-resource digests.

    Reads only resource metadata/YAML, never Secret data.
    """
    facts: dict[str, object] = {}
    code, out = sh(["kubectl", "-n", namespace, "get", "sandboxtask", run_id,
                    "-o", "jsonpath={.status.phase} {.status.conditions}"], timeout=20)
    facts["phase"] = out.strip() if code == 0 else None
    facts["resources"] = {}
    for kind, name in (("pod", f"{run_id}-tool"), ("job", f"{run_id}-runtime"),
                       ("secret", f"{run_id}-auth"), ("service", f"{run_id}-tool")):
        code, out = sh(["kubectl", "-n", namespace, "get", kind, name,
                        "-o", "jsonpath={.metadata.uid} {.metadata.ownerReferences[0].uid}"], timeout=20)
        if code == 0:
            facts["resources"][name] = out.strip()
    return facts


def ingester_result(task_id: str) -> dict[str, object] | None:
    """Fetch the run's result record from the ingester via kubectl exec.

    Returns a summary plus the full payload text; callers decide what to store.
    The payload is agent/benchmark output (not credentials).
    """
    probe = (
        "import sqlite3,glob,json\n"
        "dbs=glob.glob('/data/*.db')\n"
        "conn=sqlite3.connect(dbs[0]); conn.row_factory=sqlite3.Row\n"
        "cur=conn.cursor()\n"
        "cur.execute('SELECT * FROM task_results WHERE task_id=?', (%r,))\n"
        "row=cur.fetchone()\n"
        "print('NO_ROW' if row is None else row['payload'])" % task_id
    )
    code, out = sh(["kubectl", "-n", "clawbox-system", "exec", "deploy/clawbox-ingester",
                    "--", "python3", "-c", probe], timeout=60)
    if code != 0 or "NO_ROW" in out:
        return None
    payload = out.strip()
    summary: dict[str, object] = {"sha256": sha256_bytes(payload.encode())}
    try:
        obj = json.loads(payload)
        summary["status"] = obj.get("status")
        summary["session_id"] = obj.get("session_id")
        metadata = obj.get("metadata") or {}
        summary["agent_exit_code"] = metadata.get("agent_exit_code")
        summary["patch_status"] = metadata.get("patch_status")
        patch = obj.get("patch") or ""
        final = obj.get("final_answer") or ""
        summary["patch_len"] = len(patch)
        summary["final_answer_len"] = len(final)
    except json.JSONDecodeError:
        summary["parse_error"] = True
    return {"summary": summary, "payload": payload}


def collect(args: argparse.Namespace) -> int:
    out_root = Path(args.out_root)
    out_dir = out_root / args.release / args.cluster / args.run
    e2e_dir = out_dir / "e2e"
    e2e_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "release": args.release,
        "cluster": args.cluster,
        "run_id": args.run,
        "generated_at": now_utc(),
        "git": git_facts(),
        "schemas": schema_facts(),
        "host": host_facts(),
        "images": {ref: image_digest(ref) for ref in args.images},
    }

    # Per-run CR + rendered child resources: save YAML under e2e/ (metadata only
    # for Secrets) and record digests. Never dump Secret stringData.
    ns = args.namespace
    for kind, name in (("sandboxtask", args.run), ("pod", f"{args.run}-tool"),
                       ("job", f"{args.run}-runtime"), ("service", f"{args.run}-tool"),
                       ("networkpolicy", f"{args.run}-default-deny")):
        path = e2e_dir / f"{kind}.{name}.yaml"
        code, out = sh(["kubectl", "-n", ns, "get", kind, name, "-o", "yaml"], timeout=25)
        if code == 0:
            if kind == "secret":
                # strip credential data; keep only names/keys/types
                lines = [ln for ln in out.splitlines() if not ln.startswith(("  data:", "  stringData:"))]
                out = "\n".join(lines) + "\n  data: (redacted: keys only, no values)\n"
            path.write_text(out, encoding="utf-8")
            manifest.setdefault("artifacts", {})[f"{kind}:{name}"] = sha256_file(path)
        else:
            manifest.setdefault("artifacts", {})[f"{kind}:{name}"] = None

    manifest["run"] = kubectl_facts(ns, args.run)

    # Result receipt from the ingester (agent/benchmark output, not credentials).
    if args.result_task:
        result = ingester_result(args.result_task)
        if result is not None:
            result_file = e2e_dir / f"result.{args.result_task}.json"
            result_file.write_text(result["payload"], encoding="utf-8")
            manifest["run"]["result"] = result["summary"]
            manifest.setdefault("artifacts", {})[f"result:{args.result_task}"] = sha256_file(result_file)
        else:
            manifest["run"]["result"] = None

    gates = []
    for gate in args.gate:
        name, status = gate.split(":", 1)
        gates.append({"gate": name, "status": status, "command": "see gate script",
                      "started_at": now_utc(), "finished_at": now_utc()})
    gate_summary = {"schema_version": SCHEMA_VERSION, "release": args.release,
                    "run_id": args.run, "gates": gates, "generated_at": now_utc()}
    (out_dir / "gate-summary.json").write_text(canonical(gate_summary) + "\n", encoding="utf-8")

    manifest["self_hash"] = sha256_bytes(canonical(manifest).encode())
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                           encoding="utf-8")
    print(f"wrote {out_dir / 'manifest.json'}")
    print(f"wrote {out_dir / 'gate-summary.json'}")
    return 0


def verify(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []

    recorded = manifest.get("self_hash")
    without = {k: v for k, v in manifest.items() if k != "self_hash"}
    recomputed = sha256_bytes(canonical(without).encode())
    if recorded != recomputed:
        problems.append(f"self_hash mismatch: recorded {recorded} recomputed {recomputed}")

    for key, expected in (manifest.get("artifacts") or {}).items():
        if expected is None:
            continue
        base = key.replace(":", ".")
        matched = False
        for sub in ("e2e",):
            for suffix in (".yaml", ".json"):
                fpath = path.parent / sub / f"{base}{suffix}"
                if fpath.exists():
                    actual = sha256_file(fpath)
                    if actual != expected:
                        problems.append(f"{sub}/{base}{suffix}: digest mismatch ({actual} != {expected})")
                    matched = True
                    break
            if matched:
                break
        if not matched:
            problems.append(f"{base}: missing from evidence dir")

    git = manifest.get("git") or {}
    code, out = sh(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    if code == 0 and git.get("sha") and out.strip() != git["sha"]:
        problems.append(f"git sha changed: recorded {git['sha']} current {out.strip()}")

    if problems:
        print("VERIFY FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"VERIFY OK: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClawBox release/evidence manifest generator")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_p = sub.add_parser("collect", help="collect and write an evidence manifest")
    collect_p.add_argument("--release", required=True)
    collect_p.add_argument("--cluster", required=True)
    collect_p.add_argument("--run", required=True)
    collect_p.add_argument("--namespace", default="clawbox-benchmarks")
    collect_p.add_argument("--result-task", default=None,
                           help="fetch this run's result receipt from the ingester")
    collect_p.add_argument("--out-root", default=str(ROOT / "release-evidence"),
                           help="evidence root (default <repo>/release-evidence)")
    collect_p.add_argument("--image", dest="images", action="append", default=[],
                           help="image ref to record digest (repeatable)")
    collect_p.add_argument("--gate", dest="gate", action="append", default=[],
                           help="gate result as name:pass|fail|blocked (repeatable)")
    collect_p.set_defaults(func=collect)

    verify_p = sub.add_parser("verify", help="re-verify an existing evidence manifest")
    verify_p.add_argument("--manifest", required=True)
    verify_p.set_defaults(func=verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
