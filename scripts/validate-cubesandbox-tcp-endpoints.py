#!/usr/bin/env python3
"""Validate CubeSandbox semantic TCP endpoints at a requested concurrency.

This is a route/identity gate, not an allocator or proxy. CubeSandbox owns
the endpoint; ClawBox only asks for it, connects to the returned address, and
checks that the Tool-side identity marker matches the intended sandbox ID.
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import shlex
import sys
import time
import uuid
from pathlib import Path

from cubesandbox import NEVER_TIMEOUT, Sandbox, Template

from clawbox.cube import CubeSandboxClient
from clawbox.experiments.openclaw_driver import (
    NativeSSHConfig,
    native_ssh_target,
    native_tool_bridge_setup_command,
    split_native_ssh_target,
)
from clawbox.experiments.ssh_credentials import generate_ssh_credentials


MARKER = "/run/clawbox-ssh/clawbox-tool-sandbox-id"


def endpoint_host(endpoint) -> str:
    _user, host, _port = split_native_ssh_target(native_ssh_target(endpoint.address))
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise AssertionError(f"endpoint host is not an IP address: {host!r}") from exc
    return host


def endpoint_record(endpoint) -> dict[str, object]:
    return {
        "sandbox_id": endpoint.sandbox_id,
        "container_port": endpoint.container_port,
        "address": endpoint.address,
    }


def run(sandbox, command: str, timeout: float = 45):
    result = sandbox.commands.run(command, timeout=timeout, cwd="/workspace")
    if result.exit_code != 0:
        raise RuntimeError(
            f"sandbox command failed with exit {result.exit_code}: {result.stderr[-2000:]}"
        )
    return result


def ssh_args(ssh: NativeSSHConfig, identity: str, known_hosts: str) -> list[str]:
    user, host, port = split_native_ssh_target(ssh.target)
    host_argument = f"[{host}]" if ":" in host else host
    return [
        "/usr/bin/ssh", "-i", identity,
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostkeys=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-p", str(port), f"{user}@{host_argument}",
    ]


def setup_runtime_ssh(runtime, endpoints, credentials, session_id: str) -> tuple[str, str]:
    home = f"/state/clawbox-route-gate/{session_id}"
    identity = f"{home}/id_ed25519"
    known_hosts = f"{home}/known_hosts"
    known = ""
    for endpoint in endpoints:
        _user, host, port = split_native_ssh_target(native_ssh_target(endpoint.address))
        known += f"[{host}]:{port} {credentials.host_public}\n"
    command = f"mkdir -p {shlex.quote(home)}; "
    for path, value in ((identity, credentials.client_private), (known_hosts, known)):
        encoded = base64.b64encode(value.encode()).decode()
        command += f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}; "
    command += f"chmod 600 {shlex.quote(identity)} {shlex.quote(known_hosts)}"
    run(runtime, command)
    return identity, known_hosts


def refresh_known_host(runtime, endpoint, credentials, known_hosts: str) -> None:
    _user, host, port = split_native_ssh_target(native_ssh_target(endpoint.address))
    value = f"[{host}]:{port} {credentials.host_public}\n"
    encoded = base64.b64encode(value.encode()).decode()
    run(runtime, f"printf %s {shlex.quote(encoded)} | base64 -d >> {shlex.quote(known_hosts)}")


def probe(runtime, endpoint, identity: str, known_hosts: str) -> dict[str, object]:
    ssh = NativeSSHConfig(
        target=native_ssh_target(endpoint.address), identity_private_key="",
        host_public_key="",
    )
    command = shlex.join([*ssh_args(ssh, identity, known_hosts), f"cat {MARKER}"])
    started = time.monotonic()
    result = runtime.commands.run(command, timeout=15, cwd="/workspace")
    return {
        "address": endpoint.address,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "seconds": time.monotonic() - started,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-template", required=True)
    parser.add_argument("--tool-template", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--control-host", default=os.environ.get("CLAWBOX_CONTROL_HOST", ""))
    parser.add_argument("--count", type=int, choices=range(1, 61), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.control_host:
        parser.error("--control-host or CLAWBOX_CONTROL_HOST is required")

    for template_id in (args.runtime_template, args.tool_template):
        status = str(Template.get(template_id).status).lower()
        if status not in {"ready", "succeeded"}:
            raise RuntimeError(f"template {template_id} is not ready: {status}")

    owner = f"route-gate-c{args.count}-{uuid.uuid4().hex}"
    credentials = generate_ssh_credentials()
    cube = CubeSandboxClient(sandbox_class=Sandbox)
    tools: list[object] = []
    endpoints_before = []
    endpoints_after = []
    runtime = None
    try:
        for index in range(args.count):
            tool = Sandbox.create(
                template=args.tool_template, timeout=NEVER_TIMEOUT,
                lifecycle={"on_timeout": "kill", "auto_resume": False},
                metadata={"clawbox.owner": owner, "clawbox.role": "tool", "clawbox.index": str(index)},
                distribution_scope=[args.node],
                env_vars={
                    "CLAWBOX_VM_ROLE": "tool",
                    "TOOL_BRIDGE_LOG_PATH": "/var/lib/clawtune/artifacts/tool-bridge.jsonl",
                    "TOOL_BRIDGE_WORKDIR": "/workspace", "TOOL_MAX_CONCURRENCY": "1",
                    "CLAWBOX_TOOL_HOST_KEY_B64": base64.b64encode(
                        credentials.host_private.encode()).decode(),
                    "CLAWBOX_TOOL_AUTHORIZED_KEY_B64": base64.b64encode(
                        (credentials.client_public + "\n").encode()).decode(),
                    "TASK_ID": owner, "CELL_ID": f"c{args.count}",
                    "CLAWBOX_REPOSITORY": "clawbox/route-gate",
                },
            )
            tools.append(tool)
            endpoint = cube.get_tcp_endpoint(tool, 2222)
            if endpoint.sandbox_id != tool.sandbox_id or endpoint.container_port != 2222:
                raise AssertionError(f"endpoint identity mismatch: {endpoint_record(endpoint)}")
            endpoints_before.append(endpoint)
            setup = tool.commands.run(
                native_tool_bridge_setup_command()
                + f"; printf %s {shlex.quote(tool.sandbox_id)} > {MARKER}",
                timeout=45, cwd="/workspace",
            )
            if setup.exit_code != 0:
                raise RuntimeError(f"Tool bridge setup failed for {tool.sandbox_id}")

        before_addresses = [endpoint.address for endpoint in endpoints_before]
        if len(set(before_addresses)) != len(before_addresses):
            raise AssertionError(f"duplicate active endpoint addresses: {before_addresses}")
        endpoint_hosts = {endpoint_host(endpoint) for endpoint in endpoints_before}
        allow_out = []
        for value in (args.control_host, *sorted(endpoint_hosts)):
            address = ipaddress.ip_address(value)
            allow_out.append(f"{address}/{32 if address.version == 4 else 128}")
        runtime = Sandbox.create(
            template=args.runtime_template, timeout=NEVER_TIMEOUT,
            lifecycle={"on_timeout": "kill", "auto_resume": False},
            metadata={"clawbox.owner": owner, "clawbox.role": "runtime"},
            distribution_scope=[args.node], env_vars={"CLAWBOX_VM_ROLE": "runtime"},
            network={"allow_out": allow_out, "deny_out": ["0.0.0.0/0"]},
        )
        identity, known_hosts = setup_runtime_ssh(
            runtime, endpoints_before, credentials, owner,
        )
        initial_probes = []
        for tool, endpoint in zip(tools, endpoints_before):
            result = probe(runtime, endpoint, identity, known_hosts)
            if result["exit_code"] != 0 or result["stdout"].strip() != tool.sandbox_id:
                raise AssertionError(
                    f"initial endpoint reached wrong Tool: expected={tool.sandbox_id!r} result={result}"
                )
            initial_probes.append(result)

        stale_probes = []
        resumed_probes = []
        for index, (tool, endpoint) in enumerate(zip(tools, endpoints_before)):
            tool.pause(wait=True, timeout=120)
            stale = probe(runtime, endpoint, identity, known_hosts)
            if stale["exit_code"] == 0:
                raise AssertionError(f"stale endpoint remained usable for index {index}: {stale}")
            stale_probes.append(stale)
            tool = cube.connect_sandbox(tool.sandbox_id)
            tools[index] = tool
            resumed = cube.get_tcp_endpoint(tool, 2222)
            if resumed.sandbox_id != tool.sandbox_id or resumed.container_port != 2222:
                raise AssertionError(f"resumed endpoint identity mismatch: {endpoint_record(resumed)}")
            if resumed.address == endpoint.address:
                raise AssertionError(f"resumed endpoint did not invalidate stale address: {endpoint.address}")
            endpoints_after.append(resumed)
            refresh_known_host(runtime, resumed, credentials, known_hosts)
            result = probe(runtime, resumed, identity, known_hosts)
            if result["exit_code"] != 0 or result["stdout"].strip() != tool.sandbox_id:
                raise AssertionError(
                    f"resumed endpoint reached wrong Tool: expected={tool.sandbox_id!r} result={result}"
                )
            resumed_probes.append(result)

        after_addresses = [endpoint.address for endpoint in endpoints_after]
        if len(set(after_addresses)) != len(after_addresses):
            raise AssertionError(f"duplicate resumed endpoint addresses: {after_addresses}")
        result = {
            "status": "PASS", "owner": owner, "count": args.count,
            "endpoint_before": [endpoint_record(item) for item in endpoints_before],
            "endpoint_after": [endpoint_record(item) for item in endpoints_after],
            "initial_identity_probes": initial_probes,
            "stale_endpoint_probes": stale_probes,
            "resumed_identity_probes": resumed_probes,
            "unique_before": len(set(before_addresses)) == len(before_addresses),
            "unique_after": len(set(after_addresses)) == len(after_addresses),
            "zero_leaks": True,
        }
        print(json.dumps(result, sort_keys=True))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    finally:
        cleanup_error: Exception | None = None
        for sandbox in [*tools, runtime] if runtime is not None else tools:
            try:
                sandbox.kill()
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        try:
            leaked = [
                item for item in Sandbox.list_v2()
                if (item.get("metadata") or {}).get("clawbox.owner") == owner
            ]
            if leaked:
                raise RuntimeError(f"route gate cleanup incomplete: {leaked}")
        except Exception as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise cleanup_error


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"route gate failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
