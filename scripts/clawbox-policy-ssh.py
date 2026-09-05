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
import uuid
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


def _openclaw_unenveloped(argv: list[str]) -> tuple[dict[str, Any], str] | None:
    """Adopt OpenClaw SSH-backend calls which cannot carry an exec envelope.

    ClawTune can put an execution envelope in the ``exec`` tool's command.
    OpenClaw's SSH filesystem bridge and backend preparation calls are emitted
    below the tool-hook boundary, so there is no command parameter for the
    plugin to wrap.  Recognize only the captured OpenClaw SSH session shape,
    mint an ID for the real SSH execution, and add the same bridge envelope
    before admission.  Arbitrary unenveloped SSH remains fail-closed.
    """
    if len(argv) < 4 or "-F" not in argv:
        return None
    alias = os.environ.get("CLAWBOX_OPENCLAW_SSH_ALIAS", "openclaw-sandbox")
    if argv[-2] != alias or argv[-2].startswith("-"):
        return None
    command = argv[-1]
    is_filesystem = "openclaw-sandbox-fs" in command
    metadata = {
        "v": 1,
        "execution_id": str(uuid.uuid4()),
        "tool_name": "filesystem" if is_filesystem else "ssh_backend_maintenance",
        "execution_scope": "agent-tool" if is_filesystem else "backend-maintenance",
        "runtime_trace_expected": False,
    }
    argv[-1] = PREFIX + json.dumps(metadata, separators=(",", ":")) + "\n" + command
    return metadata, command


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
    ssh_argv = list(sys.argv[1:])
    parsed = _envelope(ssh_argv)
    policy_url = os.environ.get("CLAWBOX_POLICY_CONTROL_URL")
    policy_token = os.environ.get("CLAWBOX_POLICY_CONTROL_TOKEN")
    session_id = os.environ.get("CLAWBOX_POLICY_SESSION_ID")
    if parsed is None:
        if os.environ.get("CLAWBOX_POLICY_REQUIRE_ENVELOPE") == "1":
            parsed = _openclaw_unenveloped(ssh_argv)
            if parsed is None:
                print("ClawBox policy rejected SSH command without an execution envelope", file=sys.stderr)
                return 125
        else:
            return subprocess.call([real_ssh, *ssh_argv])
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
        "execution_scope": str(metadata.get("execution_scope") or "agent-tool"),
        "runtime_trace_expected": bool(metadata.get("runtime_trace_expected", True)),
        "command_sha256": command_sha256,
        "prediction": _prediction(command_sha256),
        "runtime_request_at": time.time(),
    }
    try:
        admission = _post("/v1/tool/admit", request, attempts=3)
        if admission.get("decision") != "ADMIT":
            raise RuntimeError(f"unexpected policy decision: {admission.get('decision')!r}")
        if admission.get("duplicate") is True:
            # The admission ledger is idempotent, but an idempotent response
            # is not permission to execute the side effect again.  A caller
            # that lost the first response must fail closed; exact-ID result
            # validation will reject the incomplete operation and cleanup
            # will reclaim the owning sandbox.
            raise RuntimeError(
                "duplicate policy admission cannot launch a second SSH subprocess"
            )
        route = _admission_route(admission)
    except Exception as exc:
        print(f"ClawBox policy admission failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 125

    execution_started_at = time.time()
    try:
        child = subprocess.Popen([real_ssh, *_ssh_args_for_route(ssh_argv, route)])
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
