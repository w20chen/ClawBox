#!/usr/bin/env python3
"""Validate CubeSandbox-owned Tool TCP routes at a requested concurrency.

This gate exercises the same admission-scoped route contract used by the
Worker. CubeSandbox owns the mapping; ClawBox only consumes its semantic
endpoint. There is no allocator, proxy, NodePort, Redis lookup, or guest-IP
discovery here.
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
import urllib.request
import uuid
from pathlib import Path

from cubesandbox import NEVER_TIMEOUT, Sandbox, Template

from clawbox.cube import CubeSandboxClient
from clawbox.experiments.openclaw_driver import (
    NativeSSHConfig,
    native_ssh_host_key_alias,
    native_ssh_route,
    native_tool_bridge_setup_command,
    split_native_ssh_target,
)
from clawbox.experiments.policy_control import PolicyControlServer
from clawbox.experiments.ssh_credentials import generate_ssh_credentials


MARKER = "/run/clawbox-ssh/clawbox-tool-sandbox-id"
ENVELOPE = "__CBX_EXEC_1__"


def endpoint_route(endpoint, epoch: int):
    route = native_ssh_route(endpoint, epoch=epoch)
    if route.container_port != 2222:
        raise AssertionError(
            f"CubeSandbox TCP endpoint is not the Tool SSH port: {route.container_port}"
        )
    try:
        ipaddress.ip_address(route.host)
    except ValueError as exc:
        raise AssertionError(f"CubeSandbox TCP endpoint host is not an IP: {route.host!r}") from exc
    return route


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


def endpoint_ssh(endpoint, credentials) -> NativeSSHConfig:
    route = endpoint_route(endpoint, epoch=1)
    return NativeSSHConfig(
        target=route.target,
        identity_private_key=credentials.client_private,
        host_public_key=credentials.host_public,
        sandbox_id=route.sandbox_id,
        host_key_alias=native_ssh_host_key_alias(route.sandbox_id),
    )


def ssh_args(ssh: NativeSSHConfig, identity: str, known_hosts: str,
             executable: str = "/usr/bin/ssh") -> list[str]:
    user, host, port = split_native_ssh_target(ssh.target)
    host_argument = f"[{host}]" if ":" in host else host
    return [
        executable, "-i", identity,
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostkeys=no",
        "-o", f"HostKeyAlias={ssh.host_key_alias}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-p", str(port), f"{user}@{host_argument}",
    ]


def setup_runtime_ssh(runtime, entries, session_id: str) -> dict[str, tuple[str, str]]:
    """Install one client key per Tool and one stable alias per host key."""
    home = f"/state/clawbox-route-gate/{session_id}"
    known_hosts = f"{home}/known_hosts"
    known = "".join(
        f"{ssh.host_key_alias} {credentials.host_public.strip()}\n"
        for ssh, credentials in entries
    )
    command = f"mkdir -p {shlex.quote(home)}; "
    known_b64 = base64.b64encode(known.encode()).decode()
    command += (
        f"printf %s {shlex.quote(known_b64)} | base64 -d > {shlex.quote(known_hosts)}; "
    )
    files: dict[str, tuple[str, str]] = {}
    for index, (ssh, credentials) in enumerate(entries):
        identity = f"{home}/id-{index}"
        private_b64 = base64.b64encode(credentials.client_private.encode()).decode()
        command += (
            f"printf %s {shlex.quote(private_b64)} | base64 -d > {shlex.quote(identity)}; "
        )
        files[ssh.sandbox_id] = (identity, known_hosts)
    command += f"chmod 600 {shlex.quote(home)}/*"
    run(runtime, command)
    return files


def ssh_probe(runtime, ssh: NativeSSHConfig, identity: str,
              known_hosts: str, remote_command: str) -> dict[str, object]:
    command = shlex.join([*ssh_args(ssh, identity, known_hosts), remote_command])
    started = time.monotonic()
    result = runtime.commands.run(command, timeout=45, cwd="/workspace")
    return {
        "target": ssh.target,
        "seconds": time.monotonic() - started,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def ssh_config_dump(runtime, ssh: NativeSSHConfig, identity: str,
                    known_hosts: str) -> dict[str, object]:
    args = ssh_args(ssh, identity, known_hosts)
    args.insert(1, "-G")
    result = run(runtime, shlex.join(args), timeout=30)
    rendered = result.stdout.lower()
    if "stricthostkeychecking yes" not in rendered and \
            "stricthostkeychecking true" not in rendered:
        raise AssertionError("ssh -G did not retain strict host-key checking")
    if f"hostkeyalias {ssh.host_key_alias.lower()}" not in rendered:
        raise AssertionError("ssh -G did not retain the stable Tool host-key alias")
    if f"userknownhostsfile {known_hosts.lower()}" not in rendered:
        raise AssertionError("ssh -G did not retain the dedicated known_hosts file")
    return {"target": ssh.target, "output": result.stdout}


def policy_ssh_call(runtime, ssh: NativeSSHConfig, identity: str, known_hosts: str,
                    session, execution_id: str, remote_command: str):
    envelope = ENVELOPE + json.dumps(
        {"v": 1, "execution_id": execution_id, "tool_name": "exec"},
        separators=(",", ":"),
    ) + "\n" + remote_command
    command = (
        "export "
        f"CLAWBOX_POLICY_CONTROL_URL={shlex.quote(session.url)} "
        f"CLAWBOX_POLICY_CONTROL_TOKEN={shlex.quote(session.token)} "
        f"CLAWBOX_POLICY_SESSION_ID={shlex.quote(session.session_id)} "
        f"CLAWBOX_TOOL_SANDBOX_ID={shlex.quote(ssh.sandbox_id)} "
        f"CLAWBOX_SSH_HOST_KEY_ALIAS={shlex.quote(ssh.host_key_alias)} "
        "CLAWBOX_POLICY_REQUIRE_ENVELOPE=1; "
        + shlex.join([*ssh_args(ssh, identity, known_hosts, "/usr/local/bin/ssh"), envelope])
    )
    return runtime.commands.run(command, timeout=45, cwd="/workspace")


def complete_without_ssh(session, execution_id: str) -> None:
    """Drain an intentionally rejected cross-Tool admission in the gate."""
    record = next(item for item in session.records()
                  if item["request"]["execution_id"] == execution_id)
    admission = record["admission"]
    request = dict(record["request"])
    now = time.time()
    request.update(
        execution_started_at=now, ssh_reaped_at=now,
        execution_completed_at=now, exit_code=125,
        endpoint_sandbox_id=admission["sandbox_id"],
        endpoint_epoch=admission["epoch"],
        endpoint_host=admission["host"], endpoint_port=admission["port"],
    )
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    http_request = urllib.request.Request(
        session.url.rstrip("/") + "/v1/tool/complete", data=encoded, method="POST",
        headers={
            "Authorization": f"Bearer {session.token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(http_request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"rejected admission cleanup returned HTTP {response.status}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-template", required=True)
    parser.add_argument("--tool-template", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--control-host", default=os.environ.get("CLAWBOX_CONTROL_HOST", ""))
    parser.add_argument("--policy-port", type=int,
                        default=int(os.environ.get("CLAWBOX_POLICY_PORT", "18080")))
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
    cube = CubeSandboxClient(sandbox_class=Sandbox)
    tools: list[object] = []
    credentials = []
    endpoints_before = []
    endpoints_after = []
    runtime = None
    sessions = []
    route_history: list[list[dict[str, object]]] = [[] for _ in range(args.count)]
    try:
        for index in range(args.count):
            tool_credentials = generate_ssh_credentials()
            credentials.append(tool_credentials)
            tool = Sandbox.create(
                template=args.tool_template, timeout=NEVER_TIMEOUT,
                lifecycle={"on_timeout": "kill", "auto_resume": False},
                metadata={"clawbox.owner": owner, "clawbox.role": "tool",
                          "clawbox.index": str(index)},
                distribution_scope=[args.node],
                env_vars={
                    "CLAWBOX_VM_ROLE": "tool",
                    "TOOL_BRIDGE_LOG_PATH": "/var/lib/clawtune/artifacts/tool-bridge.jsonl",
                    "TOOL_BRIDGE_WORKDIR": "/workspace", "TOOL_MAX_CONCURRENCY": "1",
                    "CLAWBOX_TOOL_HOST_KEY_B64": base64.b64encode(
                        tool_credentials.host_private.encode()).decode(),
                    "CLAWBOX_TOOL_AUTHORIZED_KEY_B64": base64.b64encode(
                        (tool_credentials.client_public + "\n").encode()).decode(),
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

        ssh_before = [endpoint_ssh(endpoint, credential)
                      for endpoint, credential in zip(endpoints_before, credentials)]
        before_addresses = [endpoint.address for endpoint in endpoints_before]
        if len(set(before_addresses)) != len(before_addresses):
            raise AssertionError(f"duplicate active endpoint addresses: {before_addresses}")
        endpoint_hosts = {endpoint_route(endpoint, 1).host for endpoint in endpoints_before}
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
        ssh_files = setup_runtime_ssh(
            runtime, list(zip(ssh_before, credentials)), owner,
        )
        before_ssh_config = []
        initial_probes = []
        for tool, ssh in zip(tools, ssh_before):
            identity, known_hosts = ssh_files[ssh.sandbox_id]
            before_ssh_config.append(ssh_config_dump(runtime, ssh, identity, known_hosts))
            result = ssh_probe(runtime, ssh, identity, known_hosts, f"cat {MARKER}")
            if result["exit_code"] != 0 or result["stdout"].strip() != tool.sandbox_id:
                raise AssertionError(
                    f"initial endpoint reached wrong Tool: expected={tool.sandbox_id!r} result={result}"
                )
            initial_probes.append(result)

        server = PolicyControlServer(
            advertise_host=args.control_host, advertised_port=args.policy_port,
            bind_host="0.0.0.0", bind_port=args.policy_port,
        )
        if args.policy_port == 0:
            server.advertised_port = server.actual_port
        with server:
            tool_holders = list(tools)
            route_by_execution: list[dict[str, dict[str, object]]] = [
                {} for _ in range(args.count)
            ]
            route_epochs = [0] * args.count

            def make_admit(index: int):
                def admit(request: dict) -> dict:
                    tool = tool_holders[index]
                    state_item = next(
                        (item for item in Sandbox.list_v2()
                         if str(item.get("sandboxID") or item.get("sandbox_id"))
                         == tool.sandbox_id),
                        {},
                    )
                    if str(state_item.get("state", "")).lower() == "paused":
                        tool = cube.connect_sandbox(tool.sandbox_id)
                        tool_holders[index] = tool
                    route_epochs[index] += 1
                    endpoint = cube.get_tcp_endpoint(tool, 2222)
                    route = endpoint_route(endpoint, route_epochs[index])
                    if route.sandbox_id != tool.sandbox_id:
                        raise AssertionError(
                            f"admission endpoint reached wrong Tool: {route.sandbox_id!r}"
                        )
                    rendered = {
                        "sandbox_id": route.sandbox_id, "epoch": route.epoch,
                        "container_port": route.container_port,
                        "host": route.host, "port": route.port,
                    }
                    route_by_execution[index][request["execution_id"]] = rendered
                    route_history[index].append(rendered)
                    return {"decision": "ADMIT", **rendered}
                return admit

            def make_complete(index: int):
                def complete(request: dict) -> dict:
                    expected = route_by_execution[index].pop(request["execution_id"])
                    actual = {key: request.get(key) for key in (
                        "endpoint_sandbox_id", "endpoint_epoch",
                        "endpoint_host", "endpoint_port",
                    )}
                    wanted = {
                        "endpoint_sandbox_id": expected["sandbox_id"],
                        "endpoint_epoch": expected["epoch"],
                        "endpoint_host": expected["host"],
                        "endpoint_port": expected["port"],
                    }
                    if actual != wanted:
                        raise AssertionError(f"completion route mismatch: {actual} != {wanted}")
                    started = float(request["execution_started_at"])
                    reaped = float(request["ssh_reaped_at"])
                    completed = float(request["execution_completed_at"])
                    if not started <= reaped <= completed:
                        raise AssertionError("completion did not follow SSH reaping")
                    return {"status": "COMPLETED"}
                return complete

            for index in range(args.count):
                sessions.append(server.register(
                    f"{owner}-tool-{index}", admit=make_admit(index),
                    complete=make_complete(index),
                ))

            negative_id = f"{owner}-cross-tool"
            if args.count > 1:
                cross_route: dict[str, dict[str, object]] = {}

                def cross_admit(request: dict) -> dict:
                    endpoint = cube.get_tcp_endpoint(tool_holders[1], 2222)
                    route = endpoint_route(endpoint, 1)
                    rendered = {
                        "sandbox_id": route.sandbox_id, "epoch": route.epoch,
                        "container_port": route.container_port,
                        "host": route.host, "port": route.port,
                    }
                    cross_route[request["execution_id"]] = rendered
                    return {"decision": "ADMIT", **rendered}

                def cross_complete(request: dict) -> dict:
                    expected = cross_route.pop(request["execution_id"])
                    if request.get("endpoint_sandbox_id") != expected["sandbox_id"]:
                        raise AssertionError("cross-Tool cleanup changed endpoint identity")
                    return {"status": "COMPLETED"}

                cross_session = server.register(
                    f"{owner}-cross", admit=cross_admit, complete=cross_complete,
                )
                sessions.append(cross_session)
                negative = policy_ssh_call(
                    runtime, ssh_before[0], *ssh_files[ssh_before[0].sandbox_id],
                    cross_session, negative_id, f"cat {MARKER}",
                )
                if negative.exit_code != 125:
                    raise AssertionError(
                        f"cross-Tool endpoint was not rejected before SSH: {negative}"
                    )
                complete_without_ssh(cross_session, negative_id)

            policy_initial = []
            policy_after = []
            stale_probes = []
            resumed_probes = []
            after_ssh_config = []
            for index, (tool, ssh) in enumerate(zip(tools, ssh_before)):
                identity, known_hosts = ssh_files[ssh.sandbox_id]
                execution_before = f"{owner}-{index}-before"
                result = policy_ssh_call(
                    runtime, ssh, identity, known_hosts, sessions[index],
                    execution_before, f"cat {MARKER}",
                )
                if result.exit_code != 0 or result.stdout.strip() != tool.sandbox_id:
                    raise AssertionError(f"admitted initial route reached wrong Tool: {result}")
                policy_initial.append({"execution_id": execution_before,
                                       "stdout": result.stdout})

                tool.pause(wait=True, timeout=120)
                stale = ssh_probe(runtime, ssh, identity, known_hosts, f"cat {MARKER}")
                if stale["exit_code"] == 0:
                    raise AssertionError(f"stale endpoint remained usable for {tool.sandbox_id}: {stale}")
                stale_probes.append(stale)

                execution_after = f"{owner}-{index}-after"
                # Deliberately pass the pre-pause target. The admission response
                # must be the only source of the current route for this call.
                result = policy_ssh_call(
                    runtime, ssh, identity, known_hosts, sessions[index],
                    execution_after, f"cat {MARKER}",
                )
                if result.exit_code != 0 or result.stdout.strip() != tool.sandbox_id:
                    raise AssertionError(f"admitted resumed route reached wrong Tool: {result}")
                policy_after.append({"execution_id": execution_after,
                                     "stdout": result.stdout})
                tool = tool_holders[index]
                tools[index] = tool
                resumed = cube.get_tcp_endpoint(tool, 2222)
                if resumed.sandbox_id != tool.sandbox_id or resumed.container_port != 2222:
                    raise AssertionError(f"resumed endpoint identity mismatch: {endpoint_record(resumed)}")
                endpoints_after.append(resumed)
                resumed_ssh = endpoint_ssh(resumed, credentials[index])
                after_ssh_config.append(
                    ssh_config_dump(runtime, resumed_ssh, identity, known_hosts)
                )
                resumed_probe = ssh_probe(
                    runtime, resumed_ssh, identity, known_hosts, f"cat {MARKER}"
                )
                if resumed_probe["exit_code"] != 0 or resumed_probe["stdout"].strip() != tool.sandbox_id:
                    raise AssertionError(f"resumed endpoint reached wrong Tool: {resumed_probe}")
                resumed_probes.append(resumed_probe)

            after_addresses = [endpoint.address for endpoint in endpoints_after]
            if len(set(after_addresses)) != len(after_addresses):
                raise AssertionError(f"duplicate active resumed endpoint addresses: {after_addresses}")
            for index, history in enumerate(route_history):
                if len(history) != 2 or history[0]["sandbox_id"] != tools[index].sandbox_id \
                        or history[1]["sandbox_id"] != tools[index].sandbox_id:
                    raise AssertionError(f"admission identity history mismatch: {history}")
                if int(history[1]["epoch"]) <= int(history[0]["epoch"]):
                    raise AssertionError(f"endpoint epoch did not advance: {history}")
            if any(item["completion"] is None for session in sessions for item in session.records()):
                raise AssertionError("policy contains an incomplete execution")
            for session in sessions:
                if not session.close(timeout=5):
                    raise RuntimeError("policy session did not drain")
            sessions.clear()
            result = {
                "status": "PASS", "owner": owner, "count": args.count,
                "endpoint_before": [endpoint_record(item) for item in endpoints_before],
                "endpoint_after": [endpoint_record(item) for item in endpoints_after],
                "admission_routes": route_history,
                "initial_identity_probes": initial_probes,
                "stale_endpoint_probes": stale_probes,
                "resumed_identity_probes": resumed_probes,
                "policy_initial": policy_initial, "policy_after": policy_after,
                "ssh_config_before": before_ssh_config,
                "ssh_config_after": after_ssh_config,
                "cross_tool_rejected_before_ssh": args.count > 1,
                "unique_before": len(set(before_addresses)) == len(before_addresses),
                "unique_after": len(set(after_addresses)) == len(after_addresses),
                "zero_leaks": True,
            }
            print(json.dumps(result, sort_keys=True))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
            return 0
    finally:
        for session in sessions:
            try:
                session.close(timeout=2)
            except Exception:
                pass
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
        if cleanup_error is not None and sys.exc_info()[0] is None:
            raise cleanup_error


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"route gate failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
