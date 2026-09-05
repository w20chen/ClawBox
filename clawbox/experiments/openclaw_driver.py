"""Run OpenClaw in a Runtime CubeSandbox with native SSH tools."""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
from threading import Lock
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clawbox.replay.lifecycle import CommandResult


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Only workspace/process tools cross native SSH and require Tool admission.
# Retrieval and agent-memory tools are implemented by OpenClaw itself and stay
# in the Runtime VM even though they are visible to the same agent process.
TOOL_VM_TOOLS = ("exec", "process", "read", "write", "edit", "apply_patch")
RUNTIME_LOCAL_TOOLS = ("web_search", "web_fetch", "memory_search", "memory_get")


@dataclass(frozen=True, slots=True)
class NativeSSHConfig:
    target: str
    identity_private_key: str
    host_public_key: str
    workspace_root: str = "/workspace"


class NativeSSHRouteState:
    """Thread-safe current target shared with a running OpenClaw process."""

    def __init__(self, target: str) -> None:
        split_native_ssh_target(target)
        self._target = target
        self._lock = Lock()

    def get_target(self) -> str:
        with self._lock:
            return self._target

    def update(self, target: str) -> bool:
        split_native_ssh_target(target)
        with self._lock:
            if target == self._target:
                return False
            self._target = target
            return True


def split_native_ssh_target(target: str, *, default_port: int = 22) -> tuple[str, str, int]:
    """Return ``(user, host, port)`` for an OpenClaw SSH target.

    OpenClaw stores targets in ``user@host:port`` form, while OpenSSH itself
    wants the port as a separate ``-p`` argument.  Cube's ``get_host`` may
    return either a bare host or an already mapped ``host:port`` value, so the
    parser must preserve an explicitly returned port and avoid appending a
    second one.
    """
    value = str(target or "").strip()
    if not value:
        raise ValueError("native SSH target is empty")
    user, separator, address = value.rpartition("@")
    if not separator:
        user, address = "executor", value
    if not user or not address:
        raise ValueError(f"invalid native SSH target: {target!r}")
    if address.startswith("["):
        closing = address.find("]")
        if closing < 0:
            raise ValueError(f"invalid bracketed native SSH host: {target!r}")
        host = address[1:closing]
        suffix = address[closing + 1:]
        if suffix:
            if not suffix.startswith(":") or not suffix[1:].isdigit():
                raise ValueError(f"invalid native SSH target port: {target!r}")
            port = int(suffix[1:])
        else:
            port = default_port
    elif address.count(":") == 1:
        host, rendered_port = address.rsplit(":", 1)
        if not host or not rendered_port.isdigit():
            raise ValueError(f"invalid native SSH target port: {target!r}")
        port = int(rendered_port)
    else:
        host, port = address, default_port
    if not host or not 1 <= port <= 65535:
        raise ValueError(f"invalid native SSH target: {target!r}")
    return user, host, port


def native_ssh_target(host: str, *, port: int = 22, user: str = "executor") -> str:
    """Build the target representation used by OpenClaw's SSH sandbox."""
    rendered = str(host or "").strip()
    if not rendered:
        raise ValueError("native SSH host is empty")
    if "://" in rendered:
        rendered = rendered.split("://", 1)[1].split("/", 1)[0]
    if rendered.startswith("["):
        _user, parsed_host, parsed_port = split_native_ssh_target(
            f"{user}@{rendered}", default_port=port,
        )
        return f"{_user}@[{parsed_host}]:{parsed_port}"
    if rendered.count(":") == 1:
        _user, parsed_host, parsed_port = split_native_ssh_target(
            f"{user}@{rendered}", default_port=port,
        )
        return f"{_user}@{parsed_host}:{parsed_port}"
    if rendered.count(":") > 1:
        return f"{user}@[{rendered}]:{port}"
    return f"{user}@{rendered}:{port}"


def native_tool_bridge_setup_command() -> str:
    """Return the explicit post-create Tool SSH bootstrap command.

    Cube restores a template snapshot before applying per-sandbox environment
    variables, so the image entrypoint cannot reliably see ephemeral SSH keys.
    The setup phase therefore materializes those keys through envd and starts
    the native bridge before any Agent operation is admitted.
    """
    return (
        "set -eu; "
        "mkdir -p /run/clawbox-ssh; "
        "printf '%s' \"$CLAWBOX_TOOL_HOST_KEY_B64\" | base64 -d > /run/clawbox-ssh/host_key; "
        "printf '%s' \"$CLAWBOX_TOOL_AUTHORIZED_KEY_B64\" | base64 -d > /run/clawbox-ssh/authorized_key; "
        "chmod 600 /run/clawbox-ssh/host_key /run/clawbox-ssh/authorized_key; "
        "if ! grep -Eq ':08AE[[:space:]]' /proc/net/tcp; then "
        "nohup env TOOL_BRIDGE_HOST_KEY=/run/clawbox-ssh/host_key "
        "TOOL_BRIDGE_AUTHORIZED_KEY=/run/clawbox-ssh/authorized_key "
        "TOOL_BRIDGE_LISTEN=0.0.0.0:2222 "
        "/usr/local/bin/tool-bridge </dev/null >/var/log/tool-bridge.log 2>&1 & "
        "fi; "
        "ready=0; i=0; while [ $i -lt 50 ]; do "
        "if grep -Eq ':08AE[[:space:]]' /proc/net/tcp; then ready=1; break; fi; "
        "i=$((i + 1)); sleep 0.1; done; "
        "if [ $ready -ne 1 ]; then cat /var/log/tool-bridge.log >&2 || true; exit 1; fi"
    )


def run_openclaw(*, prompt: str, session_id: str, configuration: dict,
                 ssh: NativeSSHConfig, policy_control: Any,
                 runtime_executor: Any, output_dir: Path, timeout_seconds: int,
                 model_gateway: Any | None = None,
                 prediction_manifest: dict[str, dict[str, Any]] | None = None) -> dict:
    """Run OpenClaw while every agent tool operation uses its SSH sandbox."""
    executable = str(configuration.get("openclaw_bin") or "openclaw")
    clawtune_plugin = "/opt/clawtune/packages/clawtune-plugin"
    base_url = str(configuration.get("base_url") or os.environ.get("OPENCLAW_BASE_URL", ""))
    model = str(configuration.get("model") or os.environ.get("OPENCLAW_MODEL_REF", ""))
    key_env = str(configuration.get("api_key_env", "OPENCLAW_API_KEY"))
    if not _ENV_NAME.fullmatch(key_env):
        raise ValueError("api_key_env must be a valid environment variable name")
    api_key = os.environ.get(key_env, "")
    if not prompt.strip():
        raise ValueError("OpenClaw workload case requires a non-empty prompt")
    if not model:
        raise ValueError("OpenClaw requires a model")
    if model_gateway is None and (not base_url or not api_key):
        raise ValueError(f"OpenClaw requires base_url, model, and credential environment {key_env}")
    gateway_key_env = "CLAWBOX_MODEL_GATEWAY_TOKEN"
    upstream_url = model_gateway.url if model_gateway is not None else base_url
    upstream_key_env = gateway_key_env if model_gateway is not None else key_env
    if "api_key" in configuration:
        raise ValueError("OpenClaw API keys must come from an environment variable")

    home = f"/state/openclaw/{session_id}"
    runtime_workspace = f"{home}/runtime-workspace"
    trace_dir = f"/state/clawtune/{session_id}/traces"
    ssh_dir = f"{home}/ssh"
    identity_file = f"{ssh_dir}/id_ed25519"
    known_hosts_file = f"{ssh_dir}/known_hosts"
    prediction_file = f"/state/clawtune/{session_id}/runtime-predictions.json"
    prefix = (
        f"export HOME={shlex.quote(home)} OPENCLAW_HOME={shlex.quote(home + '/.openclaw')} "
        f"CLAWBOX_POLICY_CONTROL_URL={shlex.quote(policy_control.url)} "
        f"CLAWBOX_POLICY_CONTROL_TOKEN={shlex.quote(policy_control.token)} "
        f"CLAWBOX_POLICY_SESSION_ID={shlex.quote(session_id)} "
        "CLAWBOX_POLICY_REQUIRE_ENVELOPE=1 "
        f"CLAWBOX_RUNTIME_PREDICTION_FILE={shlex.quote(prediction_file)} "
        f"CLAWTUNE_RUN_ID={shlex.quote(session_id)} CLAWTUNE_SESSION_ID={shlex.quote(session_id)}; "
    )

    def invoke(args: list[str], *, input_value: str | None = None) -> CommandResult:
        argv = " ".join(
            f'"${{{item.removeprefix("$ENV:")}}}"' if item.startswith("$ENV:")
            else shlex.quote(item) for item in [executable, *args]
        )
        if input_value is not None:
            encoded = base64.b64encode(input_value.encode()).decode()
            command = prefix + f"printf %s {shlex.quote(encoded)} | base64 -d | {argv}"
        else:
            command = prefix + argv
        result = runtime_executor.execute(command, timeout_seconds)
        if result.exit_code:
            raise RuntimeError(f"OpenClaw Runtime VM command failed: {result.stderr[-2000:]}")
        return result

    private_b64 = base64.b64encode(ssh.identity_private_key.encode()).decode()
    _user, host, port = split_native_ssh_target(ssh.target)
    known_host = f"[{host}]:{port} {ssh.host_public_key.strip()}\n"
    known_b64 = base64.b64encode(known_host.encode()).decode()
    encoded_predictions = base64.b64encode(json.dumps(
        prediction_manifest or {}, sort_keys=True, separators=(",", ":"),
    ).encode()).decode()
    setup = runtime_executor.execute(
        prefix
        + f"mkdir -p {shlex.quote(runtime_workspace)} {shlex.quote(trace_dir + '/tool-resource')} "
        + f"{shlex.quote(home + '/logs')} {shlex.quote(ssh_dir)}; "
        + f"printf %s {shlex.quote(private_b64)} | base64 -d > {shlex.quote(identity_file)}; "
        + f"printf %s {shlex.quote(known_b64)} | base64 -d > {shlex.quote(known_hosts_file)}; "
        + f"chmod 600 {shlex.quote(identity_file)} {shlex.quote(known_hosts_file)}; "
        + f"printf %s {shlex.quote(encoded_predictions)} | base64 -d > {shlex.quote(prediction_file)}; "
        + f"cp -n /opt/clawtune/cold-start/tool-resource/*-kb.json {shlex.quote(trace_dir + '/tool-resource')}/ 2>/dev/null || true; "
        + "export CLAWTUNE_POLICY=observe-only "
        + f"CLAWTUNE_TRACE_DIR={shlex.quote(trace_dir)} "
        + f"CLAWTUNE_TOOL_RESOURCE_ARTIFACT_DIR={shlex.quote(trace_dir + '/tool-resource')} "
        + "CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=false "
        + "CLAWTUNE_REPO_KEY=\"${CLAWBOX_REPO_KEY:-unknown}\" "
        + f"CLAWTUNE_LLM_UPSTREAM_BASE_URL={shlex.quote(upstream_url)} "
        + f"CLAWTUNE_LLM_UPSTREAM_API_KEY=\"${{{upstream_key_env}}}\" "
        + f"CLAWTUNE_LLM_PROXY_EXPOSE_MODEL={shlex.quote(model)} "
        + f"CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL={shlex.quote(model)}; "
        + "nohup /opt/clawtune/venv/bin/python -m clawtune_sidecar.main "
        + f"--host 127.0.0.1 --port 8765 >{shlex.quote(home + '/logs/sidecar.log')} 2>&1 & "
        + f"echo $! >{shlex.quote(home + '/sidecar.pid')}", 30,
    )
    if setup.exit_code:
        raise RuntimeError(f"ClawTune sidecar setup failed: {setup.stderr[-2000:]}")
    ready = runtime_executor.execute(
        prefix + "for i in $(seq 1 120); do curl -fsS http://127.0.0.1:8765/health/ready "
        + ">/dev/null 2>&1 && exit 0; sleep 0.5; done; exit 1", 70,
    )
    if ready.exit_code:
        raise RuntimeError("ClawTune sidecar did not become ready in the Runtime VM")
    invoke(["plugins", "install", "--link", clawtune_plugin])
    invoke(["plugins", "enable", "clawtune"])
    patch = {
        "agents": {"defaults": {"workspace": runtime_workspace, "sandbox": {
            "mode": "all", "backend": "ssh", "scope": "shared", "workspaceAccess": "rw",
            "ssh": {"target": ssh.target, "workspaceRoot": ssh.workspace_root,
                    "identityFile": identity_file, "knownHostsFile": known_hosts_file,
                    "strictHostKeyChecking": True, "updateHostKeys": False},
        }}},
        "tools": {
            "allow": [*TOOL_VM_TOOLS, *RUNTIME_LOCAL_TOOLS],
            "deny": ["browser", "canvas", "nodes", "cron", "gateway"],
            "exec": {"host": "sandbox", "security": "full", "ask": "off"},
            "elevated": {"enabled": False},
            "sandbox": {"tools": {
                "allow": list(TOOL_VM_TOOLS),
                "deny": ["browser", "canvas", "nodes", "cron", "gateway"],
            }},
        },
        "plugins": {"entries": {"clawtune": {"enabled": True, "config": {
            "endpoint": "http://127.0.0.1:8765", "mode": "observe", "failOpen": False,
            "executionBackend": "hook-only", "sandboxExecEnvelope": True,
            "instrumentHosts": ["sandbox"],
            "instrumentTools": list(TOOL_VM_TOOLS),
            "enableCgroup": False, "enableAffinity": False, "enableNuma": False,
            "autoStartSidecar": False, "securityBoundaryAccepted": True,
            "trace": {"schema_version": 6, "include_raw_events": True,
                      "include_llm_messages": True, "include_tool_outputs": True,
                      "redact_sensitive_data": True, "flush_span_start": True,
                      "trace_dir": trace_dir},
        }}}},
    }
    invoke(["config", "patch", "--stdin"], input_value=json.dumps(patch))
    invoke(["onboard", "--non-interactive", "--accept-risk", "--skip-health", "--mode", "local",
            "--auth-choice", "vllm", "--custom-base-url", "http://127.0.0.1:8765/v1",
            "--custom-api-key", f"$ENV:{upstream_key_env}", "--custom-model-id", model])
    instruction = (
        "Use only sandboxed exec/process/read/write/edit/apply_patch for workspace and "
        "process operations; those execute in the Tool VM. Web search/fetch and agent "
        "memory lookup remain Runtime-local and must not be used to access the mutable "
        "workspace.\n\nTask:\n" + prompt
    )
    result = invoke(["agent", "--local", "--agent", "main", "--session-id", session_id,
                     "--model", f"vllm/{model}", "--message", instruction,
                     "--timeout", str(timeout_seconds), "--json"])
    host_home = output_dir / "openclaw" / session_id
    host_home.mkdir(parents=True, exist_ok=True)
    (host_home / "final-answer.json").write_text(result.stdout, encoding="utf-8")
    runtime_executor.execute(
        prefix + f"if [ -s {shlex.quote(home + '/sidecar.pid')} ]; then "
        + f"kill -TERM $(cat {shlex.quote(home + '/sidecar.pid')}) 2>/dev/null || true; fi", 10,
    )
    listing = runtime_executor.execute(
        prefix + f"find {shlex.quote(trace_dir)} -type f "
        + "\\( -name '*.jsonl' -o -name '*.json' \\) -print", 30,
    )
    copied: list[str] = []
    host_trace_dir = output_dir / "runtime-traces" / session_id
    host_trace_dir.mkdir(parents=True, exist_ok=True)
    for remote_path in listing.stdout.splitlines():
        if not remote_path.startswith(trace_dir + "/"):
            continue
        relative = Path(remote_path.removeprefix(trace_dir + "/"))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        encoded = runtime_executor.execute(prefix + f"base64 -w0 {shlex.quote(remote_path)}", 30)
        if encoded.exit_code == 0:
            target = host_trace_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(encoded.stdout))
            copied.append(str(target))
    control_records = policy_control.records()
    completed = [item for item in control_records if item["completion"]]
    latencies = [
        max(0.0, float(item["completion"]["execution_completed_at"])
            - float(item["completion"]["execution_started_at"])) for item in completed
    ]
    admission_round_trips = [
        float(item["completion"].get("admission_round_trip_seconds", 0.0))
        for item in completed
    ]
    admission_control_overheads = [
        max(
            0.0,
            float(item["completion"].get("admission_round_trip_seconds", 0.0))
            - float(item["admission"].get("admission_blocked_seconds", 0.0))
            - float(item["admission"].get("restore_seconds", 0.0)),
        )
        for item in completed
    ]
    return {"stdout": result.stdout, "stderr": result.stderr,
            "tool_calls": len(completed), "tool_latencies": latencies,
            "admission_round_trip_seconds": admission_round_trips,
            "admission_control_overhead_seconds": admission_control_overheads,
            "runtime_traces": copied, "policy_control_records": control_records,
            "model_gateway_records": model_gateway.records() if model_gateway else [],
            "model_gateway_completeness": model_gateway.replay_completeness() if model_gateway else None}
