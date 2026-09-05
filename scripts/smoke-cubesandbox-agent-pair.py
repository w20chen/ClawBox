#!/usr/bin/env python3
"""Verify one Runtime + Tool CubeSandbox pair through native SSH.

This is a Cube-only boundary smoke. It uses the host policy control listener
for metadata admission while SSH carries commands and stdio directly to the
Tool VM. No WorkerBridge, NodePort, or HTTP command dispatcher is involved.
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import shlex
import sys
import time
import uuid
from pathlib import Path

from cubesandbox import NEVER_TIMEOUT, Sandbox, Template

from clawbox.cube import CubeSandboxClient
from clawbox.cube.api_retry import read_with_backoff
from clawbox.experiments.openclaw_driver import (
    NativeSSHConfig,
    native_ssh_host_key_alias,
    native_ssh_route,
    native_ssh_target,
    native_tool_bridge_setup_command,
    split_native_ssh_target,
)
from clawbox.experiments.policy_control import PolicyControlServer
from clawbox.experiments.ssh_credentials import generate_ssh_credentials


ARTIFACT_ROOT = "/var/lib/clawtune/artifacts/tool-resource/"
BRIDGE_LOG = "/var/lib/clawtune/artifacts/tool-bridge.jsonl"
ENVELOPE = "__CBX_EXEC_1__"


def listed(sandbox_id: str) -> dict | None:
    return next(
        (
            item
            for item in read_with_backoff(Sandbox.list_v2, label="Sandbox.list_v2")
            if str(item.get("sandboxID") or item.get("sandbox_id")) == sandbox_id
        ),
        None,
    )


def state(sandbox_id: str) -> str:
    return str((listed(sandbox_id) or {}).get("state", "")).lower()


def require_state(sandbox_id: str, expected: str, label: str) -> None:
    actual = state(sandbox_id)
    if actual != expected:
        raise AssertionError(f"{label} is {actual!r}, expected {expected!r}")


def endpoint_host(endpoint) -> str:
    """Extract the destination host from CubeSandbox's semantic TCP address."""
    _user, host, _port = split_native_ssh_target(
        native_ssh_target(endpoint.address)
    )
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise AssertionError(f"CubeSandbox TCP endpoint host is not an IP: {host!r}") from exc
    return host


def endpoint_ssh(endpoint, credentials) -> NativeSSHConfig:
    return NativeSSHConfig(
        target=native_ssh_target(endpoint.address),
        identity_private_key=credentials.client_private,
        host_public_key=credentials.host_public,
        sandbox_id=endpoint.sandbox_id,
        host_key_alias=native_ssh_host_key_alias(endpoint.sandbox_id),
    )


def endpoint_record(endpoint) -> dict[str, object]:
    return {
        "sandbox_id": endpoint.sandbox_id,
        "container_port": endpoint.container_port,
        "address": endpoint.address,
    }


def run(runtime, command: str, *, timeout: float = 45):
    result = runtime.commands.run(command, timeout=timeout, cwd="/workspace")
    if result.exit_code != 0:
        raise RuntimeError(
            f"Runtime command failed with exit {result.exit_code}: {result.stderr[-2000:]}"
        )
    return result


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


def setup_runtime_ssh(runtime, ssh: NativeSSHConfig, credentials,
                      session_id: str) -> tuple[str, str]:
    home = f"/state/clawbox-pair/{session_id}"
    identity = f"{home}/id_ed25519"
    known_hosts = f"{home}/known_hosts"
    known_host = f"{ssh.host_key_alias} {credentials.host_public}\n"
    command = f"mkdir -p {shlex.quote(home)}; "
    for path, value in ((identity, credentials.client_private),
                        (known_hosts, known_host)):
        encoded = base64.b64encode(value.encode()).decode()
        command += f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}; "
    command += f"chmod 600 {shlex.quote(identity)} {shlex.quote(known_hosts)}"
    run(runtime, command)
    return identity, known_hosts


def refresh_known_hosts(runtime, ssh: NativeSSHConfig, credentials,
                        known_hosts: str) -> None:
    known_host = f"{ssh.host_key_alias} {credentials.host_public}\n"
    encoded = base64.b64encode(known_host.encode()).decode()
    run(runtime, f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(known_hosts)}")


def direct_ssh_probe(runtime, ssh: NativeSSHConfig, identity: str,
                     known_hosts: str, remote_command: str = "true") -> dict:
    command = shlex.join([*ssh_args(ssh, identity, known_hosts), remote_command])
    started = time.monotonic()
    result = run(runtime, command, timeout=45)
    return {"target": ssh.target, "seconds": time.monotonic() - started,
            "exit_code": result.exit_code, "stdout": result.stdout,
            "stderr": result.stderr}


def failed_ssh_probe(runtime, ssh: NativeSSHConfig, identity: str,
                     known_hosts: str, remote_command: str) -> dict:
    command = shlex.join([*ssh_args(ssh, identity, known_hosts), remote_command])
    started = time.monotonic()
    result = runtime.commands.run(command, timeout=45, cwd="/workspace")
    return {"target": ssh.target, "seconds": time.monotonic() - started,
            "exit_code": result.exit_code, "stdout": result.stdout,
            "stderr": result.stderr}


def ssh_config_dump(runtime, ssh: NativeSSHConfig, identity: str,
                    known_hosts: str) -> dict[str, object]:
    """Inspect the effective OpenSSH policy without opening a connection."""
    args = ssh_args(ssh, identity, known_hosts)
    args.insert(1, "-G")
    command = shlex.join(args)
    result = run(runtime, command, timeout=30)
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


def unwrapped_ssh_call(runtime, ssh: NativeSSHConfig, identity: str,
                       known_hosts: str, session):
    command = (
        "export "
        f"CLAWBOX_POLICY_CONTROL_URL={shlex.quote(session.url)} "
        f"CLAWBOX_POLICY_CONTROL_TOKEN={shlex.quote(session.token)} "
        f"CLAWBOX_POLICY_SESSION_ID={shlex.quote(session.session_id)} "
        f"CLAWBOX_TOOL_SANDBOX_ID={shlex.quote(ssh.sandbox_id)} "
        f"CLAWBOX_SSH_HOST_KEY_ALIAS={shlex.quote(ssh.host_key_alias)} "
        "CLAWBOX_POLICY_REQUIRE_ENVELOPE=1; "
        + shlex.join([*ssh_args(ssh, identity, known_hosts, "/usr/local/bin/ssh"),
                      "printf should-not-run"])
    )
    return runtime.commands.run(command, timeout=45, cwd="/workspace")


def read_jsonl(raw: str, label: str) -> list[dict]:
    records = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{label}:{line_number} is not an object")
        records.append(value)
    return records


def validate_tool_artifacts(tool, policy_session, expected_ids: set[str]) -> dict:
    bridge = read_jsonl(tool.files.read(BRIDGE_LOG), BRIDGE_LOG)
    agent_records = [
        item for item in bridge if item.get("execution_source") == "runtime-envelope"
    ]
    by_id: dict[str, list[dict]] = {}
    for item in agent_records:
        by_id.setdefault(str(item.get("execution_id") or ""), []).append(item)
    if set(by_id) != expected_ids:
        raise AssertionError(
            f"Tool bridge IDs differ: expected={sorted(expected_ids)} "
            f"actual={sorted(by_id)}"
        )
    if any(len(items) != 1 for items in by_id.values()):
        raise AssertionError("Tool bridge contains duplicate runtime-envelope execution IDs")
    for execution_id in sorted(expected_ids):
        record = by_id[execution_id][0]
        if record.get("telemetry_state") != "complete":
            raise AssertionError(f"{execution_id}: incomplete Tool telemetry")
        if record.get("telemetry_collection_validity") != "valid":
            raise AssertionError(f"{execution_id}: invalid Tool telemetry collection")
        if record.get("telemetry_cleanup") != "ok":
            raise AssertionError(f"{execution_id}: Tool telemetry cleanup failed")
        if int(record.get("telemetry_loss_total") or 0) != 0:
            raise AssertionError(f"{execution_id}: Tool telemetry loss is non-zero")
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", execution_id)
        cgroup = json.loads(tool.files.read(f"{ARTIFACT_ROOT}cgroup-resource-{safe_id}.json"))
        if cgroup.get("execution_id") != execution_id or cgroup.get("source") != "cgroup-v2":
            raise AssertionError(f"{execution_id}: invalid cgroup identity/source: {cgroup}")
        if cgroup.get("sampling_quality") != "valid":
            raise AssertionError(f"{execution_id}: invalid cgroup sampling")
        artifact_path = str(record.get("telemetry_artifact") or "")
        if not artifact_path.startswith(ARTIFACT_ROOT):
            raise AssertionError(f"{execution_id}: unsafe telemetry path")
        telemetry = json.loads(tool.files.read(artifact_path))
        calls = telemetry.get("calls") or []
        if len(calls) != 1 or calls[0].get("tool_call_id") != execution_id:
            raise AssertionError(f"{execution_id}: invalid telemetry call identity")
    policy_records = policy_session.records()
    if {item["request"]["execution_id"] for item in policy_records} != expected_ids:
        raise AssertionError("policy and expected execution IDs differ")
    if any(item["completion"] is None for item in policy_records):
        raise AssertionError("policy contains an incomplete execution")
    return {
        "exact_id_join_rate": 1.0,
        "execution_ids": sorted(expected_ids),
        "bridge_runtime_envelope_records": len(agent_records),
        "telemetry_loss_total": 0,
        "duplicate_tool_execution_count": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-template", required=True)
    parser.add_argument("--tool-template", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument(
        "--control-host", default=os.environ.get("CLAWBOX_CONTROL_HOST", ""),
        help="host IP reachable from Runtime for the metadata-only policy listener",
    )
    parser.add_argument("--policy-port", type=int,
                        default=int(os.environ.get("CLAWBOX_POLICY_PORT", "18080")))
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if not args.control_host:
        parser.error("--control-host or CLAWBOX_CONTROL_HOST is required")
    owner = f"pair-smoke-{uuid.uuid4().hex}"
    session_id = f"{owner}-session"
    credentials = generate_ssh_credentials()
    cube = CubeSandboxClient(sandbox_class=Sandbox)
    runtime = None
    tool = None
    policy_session = None
    expected_ids: set[str] = set()
    restore_count = 0
    restore_seconds: list[float] = []
    admission_routes: list[dict[str, object]] = []
    tool_holder: dict[str, object] = {}
    timings: dict[str, float] = {}
    try:
        for template_id in (args.runtime_template, args.tool_template):
            status = str(Template.get(template_id).status).lower()
            if status not in {"ready", "succeeded"}:
                raise RuntimeError(f"template {template_id} is not ready: {status}")
        started = time.monotonic()
        tool = Sandbox.create(
            template=args.tool_template, timeout=NEVER_TIMEOUT,
            lifecycle={"on_timeout": "kill", "auto_resume": False},
            metadata={"clawbox.owner": owner, "clawbox.role": "tool"},
            distribution_scope=[args.node],
            env_vars={
                "CLAWBOX_VM_ROLE": "tool", "TOOL_BRIDGE_LOG_PATH": BRIDGE_LOG,
                "TOOL_BRIDGE_WORKDIR": "/workspace", "TOOL_MAX_CONCURRENCY": "1",
                "CLAWBOX_TOOL_HOST_KEY_B64": base64.b64encode(
                    credentials.host_private.encode()).decode(),
                "CLAWBOX_TOOL_AUTHORIZED_KEY_B64": base64.b64encode(
                    (credentials.client_public + "\n").encode()).decode(),
                "TASK_ID": owner, "CELL_ID": "pair-smoke",
                "CLAWBOX_REPOSITORY": "clawbox/pair-smoke",
            },
        )
        timings["tool_create_seconds"] = time.monotonic() - started
        tool_holder["sandbox"] = tool
        tool_id = tool.sandbox_id
        setup_result = tool.commands.run(
            native_tool_bridge_setup_command(), timeout=30, cwd="/workspace",
        )
        if setup_result.exit_code != 0:
            raise RuntimeError(
                "Tool setup could not start native SSH bridge: "
                + setup_result.stderr[-1000:]
            )
        marker = "/run/clawbox-ssh/clawbox-tool-sandbox-id"
        marker_result = tool.commands.run(
            f"printf %s {shlex.quote(tool_id)} > {marker}",
            timeout=30, cwd="/workspace",
        )
        if marker_result.exit_code != 0:
            raise RuntimeError("Tool identity marker setup failed")
        endpoint_before = cube.get_tcp_endpoint(tool, 2222)
        endpoint_host_value = endpoint_host(endpoint_before)
        runtime_allow_out = []
        for value in (args.control_host, endpoint_host_value):
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise AssertionError(f"expected an IP address, got {value!r}") from exc
            runtime_allow_out.append(
                f"{address}/{32 if address.version == 4 else 128}"
            )
        runtime_allow_out = list(dict.fromkeys(runtime_allow_out))

        started = time.monotonic()
        runtime = Sandbox.create(
            template=args.runtime_template, timeout=NEVER_TIMEOUT,
            lifecycle={"on_timeout": "kill", "auto_resume": False},
            metadata={"clawbox.owner": owner, "clawbox.role": "runtime"},
            distribution_scope=[args.node], env_vars={"CLAWBOX_VM_ROLE": "runtime"},
            network={"allow_out": runtime_allow_out, "deny_out": ["0.0.0.0/0"]},
        )
        timings["runtime_create_seconds"] = time.monotonic() - started
        ssh = endpoint_ssh(endpoint_before, credentials)
        initial_ssh = ssh
        identity, known_hosts = setup_runtime_ssh(runtime, ssh, credentials, session_id)
        ssh_policy_before = ssh_config_dump(runtime, ssh, identity, known_hosts)
        probe = direct_ssh_probe(runtime, ssh, identity, known_hosts, f"cat {marker}")
        if probe["stdout"].strip() != tool_id:
            raise AssertionError(
                f"initial native SSH reached the wrong Tool: expected={tool_id!r} "
                f"actual={probe['stdout']!r}"
            )

        server = PolicyControlServer(
            advertise_host=args.control_host, advertised_port=args.policy_port,
            bind_host="0.0.0.0", bind_port=args.policy_port,
        )
        if args.policy_port == 0:
            server.advertised_port = server.actual_port
        with server:
            def admit(request: dict) -> dict:
                nonlocal restore_count
                current_tool = tool_holder["sandbox"]
                if state(current_tool.sandbox_id) == "paused":
                    started_restore = time.monotonic()
                    current_tool = cube.connect_sandbox(current_tool.sandbox_id)
                    tool_holder["sandbox"] = current_tool
                    restore_seconds.append(time.monotonic() - started_restore)
                    restore_count += 1
                route = native_ssh_route(
                    cube.get_tcp_endpoint(current_tool, 2222),
                    epoch=len(admission_routes) + 1,
                )
                if route.sandbox_id != tool_id:
                    raise AssertionError(
                        f"admission route reached wrong Tool: {route.sandbox_id!r}"
                    )
                rendered = {
                    "sandbox_id": route.sandbox_id, "epoch": route.epoch,
                    "container_port": route.container_port,
                    "host": route.host, "port": route.port,
                }
                admission_routes.append(rendered)
                return {"decision": "ADMIT", "restore_count": restore_count, **rendered}

            def complete(_request: dict) -> dict:
                return {"status": "COMPLETED"}

            policy_session = server.register(session_id, admit=admit, complete=complete)
            before_id = f"{session_id}-before-{uuid.uuid4().hex}"
            after_id = f"{session_id}-after-{uuid.uuid4().hex}"
            expected_ids.add(before_id)
            result = policy_ssh_call(
                runtime, initial_ssh, identity, known_hosts, policy_session, before_id,
                f"cat {marker}",
            )
            if result.exit_code != 0 or result.stdout.strip() != tool_id:
                raise AssertionError(f"native SSH reached the wrong Tool before pause: {result}")
            unwrapped = unwrapped_ssh_call(
                runtime, initial_ssh, identity, known_hosts, policy_session,
            )
            if unwrapped.exit_code != 125:
                raise AssertionError(f"unenveloped Agent operation was not rejected: {unwrapped}")

            started = time.monotonic()
            tool.pause(wait=True, timeout=90)
            timings["tool_pause_seconds"] = time.monotonic() - started
            require_state(tool_id, "paused", "Tool after checkpoint")
            require_state(runtime.sandbox_id, "running", "Runtime while Tool paused")
            paused_endpoint = cube.get_tcp_endpoint(tool, 2222)
            if paused_endpoint.sandbox_id != tool_id:
                raise AssertionError("paused endpoint identity changed")
            stale_probe = failed_ssh_probe(
                runtime, initial_ssh, identity, known_hosts, f"cat {marker}",
            )
            if stale_probe["exit_code"] == 0:
                raise AssertionError(
                    "stale native SSH endpoint remained usable while Tool was paused: "
                    f"{stale_probe}"
                )

            started = time.monotonic()
            tool = cube.connect_sandbox(tool_id)
            tool_holder["sandbox"] = tool
            restore_seconds.append(time.monotonic() - started)
            restore_count += 1
            endpoint_after = cube.get_tcp_endpoint(tool, 2222)
            if endpoint_after.sandbox_id != tool_id:
                raise AssertionError("resumed endpoint identity changed")
            ssh = endpoint_ssh(endpoint_after, credentials)
            refresh_known_hosts(runtime, ssh, credentials, known_hosts)
            resumed_probe = direct_ssh_probe(
                runtime, ssh, identity, known_hosts, f"cat {marker}",
            )
            ssh_policy_after = ssh_config_dump(runtime, ssh, identity, known_hosts)
            if resumed_probe["stdout"].strip() != tool_id:
                raise AssertionError(
                    f"resumed native SSH reached the wrong Tool: {resumed_probe}"
                )

            expected_ids.add(after_id)
            result = policy_ssh_call(
                runtime, initial_ssh, identity, known_hosts, policy_session, after_id,
                f"cat {marker}",
            )
            if result.exit_code != 0 or result.stdout.strip() != tool_id:
                raise AssertionError(f"native SSH reached the wrong Tool after restore: {result}")
            if len(admission_routes) != 2:
                raise AssertionError(f"expected two admission endpoint resolutions: {admission_routes}")
            if admission_routes[0]["sandbox_id"] != tool_id or admission_routes[1]["sandbox_id"] != tool_id:
                raise AssertionError(f"admission endpoint identity mismatch: {admission_routes}")
            if int(admission_routes[1]["epoch"]) <= int(admission_routes[0]["epoch"]):
                raise AssertionError(f"endpoint epoch did not advance after restore: {admission_routes}")
            timings["tool_restore_seconds"] = restore_seconds[-1]
            require_state(tool_id, "running", "Tool after demand restore")
            require_state(runtime.sandbox_id, "running", "Runtime after Tool restore")

            tool = tool_holder["sandbox"]
            validation = validate_tool_artifacts(tool, policy_session, expected_ids)
            policy_json = json.dumps(server.requests, sort_keys=True)
            if any(secret in policy_json for secret in (tool_id, "should-not-run")):
                raise AssertionError("policy control records contain command/output data")
            if not policy_session.close(timeout=5):
                raise RuntimeError("policy session did not drain")
            policy_session = None
            payload = {
                "status": "PASS", "owner": owner,
                "runtime_id": runtime.sandbox_id, "tool_id": tool_id,
                "tcp_endpoint_before": endpoint_record(endpoint_before),
                "tcp_endpoint_paused": endpoint_record(paused_endpoint),
                "tcp_endpoint_after": endpoint_record(endpoint_after),
                "admission_routes": admission_routes,
                "stale_endpoint_probe": stale_probe,
                "native_identity": {
                    "expected_tool_id": tool_id,
                    "before": probe["stdout"].strip(),
                    "after": resumed_probe["stdout"].strip(),
                },
                "policy_endpoint": server.url, "native_probe": probe,
                "ssh_config_before": ssh_policy_before,
                "ssh_config_after": ssh_policy_after,
                "restore_count": restore_count, "timings": timings,
                "telemetry": validation, "exact_id_validation": True,
            }
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(payload, sort_keys=True) + "\n")
            print(json.dumps(payload, sort_keys=True))
            return 0
    finally:
        if policy_session is not None:
            try:
                policy_session.close(timeout=2)
            except Exception:
                pass
        cleanup_error: Exception | None = None
        for sandbox in (tool, runtime):
            if sandbox is None:
                continue
            try:
                read_with_backoff(lambda sandbox=sandbox: sandbox.kill(),
                                  label=f"kill {sandbox.sandbox_id}", attempts=4)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        try:
            remaining = read_with_backoff(Sandbox.list_v2, label="final owner audit")
            leaked = [
                item for item in remaining
                if (item.get("metadata") or {}).get("clawbox.owner") == owner
            ]
            if leaked:
                raise RuntimeError(f"pair smoke cleanup incomplete for owner {owner}: {leaked}")
        except Exception as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None and sys.exc_info()[0] is None:
            raise cleanup_error


if __name__ == "__main__":
    raise SystemExit(main())
