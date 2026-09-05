#!/usr/bin/env python3
"""OpenSSH shim that synchronously gates ClawTune-enveloped tool calls.

The shim inherits stdin/stdout/stderr unchanged and launches the real OpenSSH
client exactly once after admission.  HTTP carries control metadata only.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PREFIX = "__CBX_EXEC_1__"
_HOST_KEY_ALIAS = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _envelope(argv: list[str]) -> tuple[dict[str, Any], str] | None:
    for argument in argv:
        marker = argument.find(PREFIX)
        if marker < 0:
            continue
        rest = argument[marker + len(PREFIX):]
        header, separator, payload = rest.partition("\n")
        if not separator:
            continue
        try:
            metadata = json.loads(header)
        except json.JSONDecodeError:
            metadata = {"v": 1, "execution_id": header.strip()}
        if metadata.get("v") != 1 or not metadata.get("execution_id"):
            continue
        command = argument[:marker] + payload
        return metadata, command
    return None


def _prediction(command_sha256: str) -> dict[str, Any] | None:
    source = os.environ.get("CLAWBOX_RUNTIME_PREDICTION_FILE", "")
    if not source:
        return None
    try:
        value = json.loads(Path(source).read_text(encoding="utf-8"))
        prediction = value.get(command_sha256)
        return prediction if isinstance(prediction, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _post(path: str, body: dict[str, Any], *, attempts: int) -> dict[str, Any]:
    base = os.environ["CLAWBOX_POLICY_CONTROL_URL"].rstrip("/")
    token = os.environ["CLAWBOX_POLICY_CONTROL_TOKEN"]
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    request = urllib.request.Request(
        base + path, data=encoded, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=3600) as response:
                return json.load(response)
        except urllib.error.HTTPError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.1 * (2 ** attempt))
    raise RuntimeError(f"policy control unavailable: {last_error}")


def _admission_route(admission: dict[str, Any]) -> dict[str, Any]:
    """Validate the endpoint returned for this one admitted invocation."""
    expected_sandbox_id = os.environ.get("CLAWBOX_TOOL_SANDBOX_ID", "").strip()
    sandbox_id = str(admission.get("sandbox_id") or "").strip()
    if not expected_sandbox_id or sandbox_id != expected_sandbox_id:
        raise RuntimeError(
            f"policy route belongs to {sandbox_id!r}, expected Tool {expected_sandbox_id!r}"
        )
    epoch = admission.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise RuntimeError("policy route has no positive endpoint epoch")
    container_port = admission.get("container_port")
    if (isinstance(container_port, bool) or not isinstance(container_port, int)
            or container_port != 2222):
        raise RuntimeError("policy route is not for the Tool SSH port 2222")
    host = str(admission.get("host") or "").strip()
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise RuntimeError(f"policy route host is not an IP address: {host!r}") from exc
    port = admission.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RuntimeError("policy route has an invalid TCP port")
    alias = os.environ.get("CLAWBOX_SSH_HOST_KEY_ALIAS", "").strip()
    if not _HOST_KEY_ALIAS.fullmatch(alias):
        raise RuntimeError("stable Tool SSH host-key alias is not configured")
    return {
        "sandbox_id": sandbox_id, "epoch": epoch, "host": host,
        "port": port, "host_key_alias": alias,
    }


def _ssh_args_for_route(argv: list[str], route: dict[str, Any]) -> list[str]:
    """Override only destination routing for the current OpenSSH invocation.

    OpenClaw supplies a generated SSH config and the final two arguments are
    its stable host alias plus the remote command.  The config remains the
    source of identity and strict-host-key settings; these options only make
    the admitted CubeSandbox route current for this invocation.
    """
    if len(argv) < 2:
        raise RuntimeError("SSH invocation has no destination and remote command")
    target_index = len(argv) - 2
    if argv[target_index].startswith("-"):
        raise RuntimeError("SSH invocation has no stable destination argument")
    overrides = [
        "-o", f"HostName={route['host']}",
        "-o", f"Port={route['port']}",
        "-o", f"HostKeyAlias={route['host_key_alias']}",
    ]
    return [*argv[:target_index], *overrides, *argv[target_index:]]


def main() -> int:
    real_ssh = os.environ.get("CLAWBOX_REAL_SSH", "/usr/bin/ssh")
    parsed = _envelope(sys.argv[1:])
    policy_url = os.environ.get("CLAWBOX_POLICY_CONTROL_URL")
    policy_token = os.environ.get("CLAWBOX_POLICY_CONTROL_TOKEN")
    session_id = os.environ.get("CLAWBOX_POLICY_SESSION_ID")
    if parsed is None:
        if os.environ.get("CLAWBOX_POLICY_REQUIRE_ENVELOPE") == "1":
            print("ClawBox policy rejected SSH command without an execution envelope", file=sys.stderr)
            return 125
        return subprocess.call([real_ssh, *sys.argv[1:]])
    if not policy_url or not policy_token or not session_id:
        print("ClawBox policy control is not configured", file=sys.stderr)
        return 125

    metadata, command = parsed
    execution_id = str(metadata["execution_id"])
    command_sha256 = hashlib.sha256(command.encode()).hexdigest()
    request = {
        "session_id": session_id,
        "execution_id": execution_id,
        "operation": str(metadata.get("tool_name") or "exec"),
        "command_sha256": command_sha256,
        "prediction": _prediction(command_sha256),
        "runtime_request_at": time.time(),
    }
    try:
        admission = _post("/v1/tool/admit", request, attempts=3)
        if admission.get("decision") != "ADMIT":
            raise RuntimeError(f"unexpected policy decision: {admission.get('decision')!r}")
        route = _admission_route(admission)
    except Exception as exc:
        print(f"ClawBox policy admission failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 125

    execution_started_at = time.time()
    try:
        child = subprocess.Popen([real_ssh, *_ssh_args_for_route(sys.argv[1:], route)])
        return_code = child.wait()
    except OSError as exc:
        print(f"ClawBox real SSH could not start: {exc}", file=sys.stderr)
        return_code = 127
    ssh_reaped_at = time.time()
    execution_completed_at = time.time()
    completion = {
        **request,
        "execution_started_at": execution_started_at,
        "execution_completed_at": execution_completed_at,
        "exit_code": return_code,
        "endpoint_sandbox_id": route["sandbox_id"],
        "endpoint_epoch": route["epoch"],
        "endpoint_host": route["host"],
        "endpoint_port": route["port"],
        "ssh_reaped_at": ssh_reaped_at,
    }
    try:
        _post("/v1/tool/complete", completion, attempts=3)
    except Exception as exc:
        # The command has already run. Never retry it to repair a missing
        # completion; fail the caller and let exact-ID acceptance reject the run.
        print(f"ClawBox policy completion failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 125
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
