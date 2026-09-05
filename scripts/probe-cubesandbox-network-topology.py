#!/usr/bin/env python3
"""Diagnose CubeSandbox same-node SSH routes without adding a fallback path.

This is evidence-only.  The supported Worker continues to consume only
CubeSandbox's semantic endpoint API; this probe reads CubeMaster metadata solely
to compare that endpoint with the same-node SandboxIP route.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from cubesandbox import NEVER_TIMEOUT, Sandbox, Template

from clawbox.cube import CubeSandboxClient
from clawbox.experiments.openclaw_driver import (
    NativeSSHConfig,
    native_ssh_host_key_alias,
    native_ssh_target,
    native_tool_bridge_setup_command,
    split_native_ssh_target,
)
from clawbox.experiments.ssh_credentials import generate_ssh_credentials


MARKER = "/run/clawbox-ssh/clawbox-tool-sandbox-id"


def _run(sandbox, command: str, timeout: float = 45):
    return sandbox.commands.run(command, timeout=timeout, cwd="/workspace")


def _master_info(url: str, sandbox_id: str) -> dict:
    query = urllib.parse.urlencode({
        "sandbox_id": sandbox_id,
        "instance_type": "cubebox",
        "container_port": 2222,
    })
    with urllib.request.urlopen(url.rstrip("/") + "/cube/sandbox/info?" + query, timeout=15) as response:
        payload = json.load(response)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError(f"CubeMaster returned no unique sandbox row: {payload!r}")
    return rows[0]


def _ssh_probe(runtime, ssh: NativeSSHConfig, identity: str, known_hosts: str) -> dict:
    user, host, port = split_native_ssh_target(ssh.target)
    address = f"[{host}]" if ":" in host else host
    argv = [
        "/usr/bin/ssh", "-i", identity,
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
        "-o", f"HostKeyAlias={ssh.host_key_alias}", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=3", "-p", str(port), f"{user}@{address}",
        f"cat {MARKER}",
    ]
    started = time.monotonic()
    result = _run(runtime, shlex.join(argv), 15)
    return {
        "target": ssh.target,
        "duration_seconds": time.monotonic() - started,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "identity_matches": result.exit_code == 0
        and result.stdout.strip() == ssh.sandbox_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-template", required=True)
    parser.add_argument("--tool-template", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--cube-master-url", required=True)
    parser.add_argument("--physical-host")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    for template_id in (args.runtime_template, args.tool_template):
        if str(Template.get(template_id).status).lower() != "ready":
            raise RuntimeError(f"template is not READY: {template_id}")

    owner = "topology-probe-" + uuid.uuid4().hex
    credential = generate_ssh_credentials()
    tool = runtime = None
    evidence: dict = {"owner": owner, "status": "FAILED", "zero_leaks": False}
    try:
        tool = Sandbox.create(
            template=args.tool_template, timeout=NEVER_TIMEOUT,
            lifecycle={"on_timeout": "kill", "auto_resume": False},
            metadata={"clawbox.owner": owner, "clawbox.role": "tool"},
            distribution_scope=[args.node],
            env_vars={
                "CLAWBOX_VM_ROLE": "tool",
                "CLAWBOX_TOOL_HOST_KEY_B64": base64.b64encode(
                    credential.host_private.encode()
                ).decode(),
                "CLAWBOX_TOOL_AUTHORIZED_KEY_B64": base64.b64encode(
                    (credential.client_public + "\n").encode()
                ).decode(),
                "TASK_ID": owner, "CELL_ID": "topology",
            },
        )
        setup = _run(
            tool,
            native_tool_bridge_setup_command()
            + f"; printf %s {shlex.quote(tool.sandbox_id)} > {MARKER}",
        )
        if setup.exit_code != 0:
            raise RuntimeError(f"Tool setup failed: {setup.stderr[-1000:]}")

        semantic = CubeSandboxClient(sandbox_class=Sandbox).get_tcp_endpoint(tool, 2222)
        master = _master_info(args.cube_master_url, tool.sandbox_id)
        sandbox_ip = str(master.get("sandbox_ip") or "").strip()
        if not sandbox_ip:
            raise RuntimeError("diagnostic CubeMaster row has no sandbox_ip")
        alias = native_ssh_host_key_alias(tool.sandbox_id)
        semantic_ssh = NativeSSHConfig(
            target=native_ssh_target(semantic.address),
            identity_private_key=credential.client_private,
            host_public_key=credential.host_public,
            sandbox_id=tool.sandbox_id, host_key_alias=alias,
        )
        sandbox_ssh = NativeSSHConfig(
            target=native_ssh_target(sandbox_ip, port=2222),
            identity_private_key=credential.client_private,
            host_public_key=credential.host_public,
            sandbox_id=tool.sandbox_id, host_key_alias=alias,
        )
        runtime = Sandbox.create(
            template=args.runtime_template, timeout=NEVER_TIMEOUT,
            lifecycle={"on_timeout": "kill", "auto_resume": False},
            metadata={"clawbox.owner": owner, "clawbox.role": "runtime"},
            distribution_scope=[args.node], env_vars={"CLAWBOX_VM_ROLE": "runtime"},
        )
        root = f"/state/topology/{owner}"
        private = base64.b64encode(credential.client_private.encode()).decode()
        known = base64.b64encode(
            f"{alias} {credential.host_public.strip()}\n".encode()
        ).decode()
        identity, known_hosts = f"{root}/id", f"{root}/known_hosts"
        materialize = _run(runtime, (
            f"mkdir -p {shlex.quote(root)}; "
            f"printf %s {shlex.quote(private)} | base64 -d > {shlex.quote(identity)}; "
            f"printf %s {shlex.quote(known)} | base64 -d > {shlex.quote(known_hosts)}; "
            f"chmod 600 {shlex.quote(identity)} {shlex.quote(known_hosts)}"
        ))
        if materialize.exit_code != 0:
            raise RuntimeError("Runtime SSH materialization failed")

        evidence.update({
            "semantic_endpoint": {
                "sandbox_id": semantic.sandbox_id,
                "container_port": semantic.container_port,
                "address": semantic.address,
            },
            "diagnostic_cube_master": {
                "host_ip": master.get("host_ip"),
                "sandbox_ip": sandbox_ip,
                "exposed_port_mode": master.get("exposed_port_mode"),
                "exposed_port_endpoint": master.get("exposed_port_endpoint"),
            },
            "runtime_to_semantic": _ssh_probe(
                runtime, semantic_ssh, identity, known_hosts,
            ),
            "runtime_to_sandbox_ip": _ssh_probe(
                runtime, sandbox_ssh, identity, known_hosts,
            ),
        })
        if args.physical_host:
            _semantic_user, _semantic_host, semantic_port = split_native_ssh_target(
                semantic_ssh.target
            )
            physical_ssh = NativeSSHConfig(
                target=native_ssh_target(args.physical_host, port=semantic_port),
                identity_private_key=credential.client_private,
                host_public_key=credential.host_public,
                sandbox_id=tool.sandbox_id, host_key_alias=alias,
            )
            evidence["runtime_to_physical_hostport"] = _ssh_probe(
                runtime, physical_ssh, identity, known_hosts,
            )
        evidence["same_node_sandbox_ip_supported"] = evidence[
            "runtime_to_sandbox_ip"
        ]["identity_matches"]
        evidence["same_node_hostport_hairpin_supported"] = evidence[
            "runtime_to_semantic"
        ]["identity_matches"]
        routes = {
            "semantic_hostport": evidence["runtime_to_semantic"]["identity_matches"],
            "sandbox_ip": evidence["runtime_to_sandbox_ip"]["identity_matches"],
        }
        if "runtime_to_physical_hostport" in evidence:
            routes["physical_hostport"] = evidence[
                "runtime_to_physical_hostport"
            ]["identity_matches"]
        evidence["route_identity_results"] = routes
        evidence["reachable_identity_routes"] = sorted(
            name for name, passed in routes.items() if passed
        )
        evidence["status"] = "DIAGNOSTIC_COMPLETE"
    except Exception as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for sandbox in (runtime, tool):
            if sandbox is not None:
                try:
                    sandbox.kill()
                except Exception as exc:
                    evidence.setdefault("cleanup_errors", []).append(str(exc))
        leaked = [
            item for item in Sandbox.list_v2()
            if (item.get("metadata") or {}).get("clawbox.owner") == owner
        ]
        evidence["zero_leaks"] = not leaked
        evidence["leaked_sandboxes"] = leaked
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(evidence, sort_keys=True))
    return 0 if (
        evidence["status"] == "DIAGNOSTIC_COMPLETE" and evidence["zero_leaks"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
