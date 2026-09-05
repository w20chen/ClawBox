#!/usr/bin/env python3
"""OpenSSH shim that synchronously gates ClawTune-enveloped tool calls.

The shim inherits stdin/stdout/stderr unchanged and launches the real OpenSSH
client exactly once after admission.  HTTP carries control metadata only.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PREFIX = "__CBX_EXEC_1__"


def _split_target(target: str) -> tuple[str, str, int]:
    """Parse the semantic target returned by PolicyControl."""
    value = str(target or "").strip()
    user, separator, address = value.rpartition("@")
    if not separator or not user or not address:
        raise ValueError("admission returned an invalid SSH target")
    if address.startswith("["):
        closing = address.find("]")
        if closing < 0 or not address[closing + 1:].startswith(":"):
            raise ValueError("admission returned an invalid bracketed SSH target")
        host = address[1:closing]
        rendered_port = address[closing + 2:]
    else:
        host, separator, rendered_port = address.rpartition(":")
        if not separator:
            raise ValueError("admission SSH target has no explicit port")
    if not host or not rendered_port.isdigit() or not 1 <= int(rendered_port) <= 65535:
        raise ValueError("admission returned an invalid SSH target port")
    return user, host, int(rendered_port)


def _route_argv(argv: list[str], target: str) -> list[str]:
    """Retarget the already-created OpenSSH invocation after admission.

    OpenClaw constructs the command before the policy hook runs. A Tool restore
    can change CubeSandbox's mapped port while admission is blocked, so merely
    patching OpenClaw's configuration is too late for this invocation.
    """
    user, host, port = _split_target(target)
    envelope_index = next(
        (index for index, argument in enumerate(argv) if PREFIX in argument), None,
    )
    if envelope_index is None or envelope_index == 0:
        raise ValueError("cannot locate SSH destination before execution envelope")
    destination_index = envelope_index - 1
    if argv[destination_index].startswith("-"):
        raise ValueError("cannot identify SSH destination")
    routed = list(argv)
    routed[destination_index] = f"{user}@[{host}]" if ":" in host else f"{user}@{host}"
    port_replaced = False
    index = 0
    while index < destination_index:
        argument = routed[index]
        if argument == "-p":
            if index + 1 >= destination_index:
                raise ValueError("SSH -p option has no value")
            routed[index + 1] = str(port)
            port_replaced = True
            index += 2
            continue
        if argument.startswith("-p") and len(argument) > 2:
            routed[index] = f"-p{port}"
            port_replaced = True
        index += 1
    if not port_replaced:
        routed[destination_index:destination_index] = ["-p", str(port)]
    return routed


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
    admission_request_at = time.time()
    admission_started = time.monotonic()
    try:
        admission = _post("/v1/tool/admit", request, attempts=3)
        if admission.get("decision") != "ADMIT":
            raise RuntimeError(f"unexpected policy decision: {admission.get('decision')!r}")
        routed_argv = list(sys.argv[1:])
        if admission.get("ssh_target"):
            routed_argv = _route_argv(routed_argv, str(admission["ssh_target"]))
    except Exception as exc:
        print(f"ClawBox policy admission failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 125

    admission_response_at = time.time()
    admission_round_trip_seconds = max(0.0, time.monotonic() - admission_started)
    execution_started_at = time.time()
    return_code = subprocess.call([real_ssh, *routed_argv])
    completion = {
        **request,
        "admission_request_at": admission_request_at,
        "admission_response_at": admission_response_at,
        "admission_round_trip_seconds": admission_round_trip_seconds,
        "ssh_target": admission.get("ssh_target"),
        "execution_started_at": execution_started_at,
        "execution_completed_at": time.time(),
        "exit_code": return_code,
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
