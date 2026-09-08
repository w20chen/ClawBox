"""Run OpenClaw in a Runtime CubeSandbox with native SSH tools."""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from clawbox.replay.lifecycle import CommandResult
from .runtime_model_relay import RELAY_PORT, RUNTIME_MODEL_RELAY_SCRIPT


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Workspace and process tools cross the native SSH boundary. Retrieval and
# agent-memory tools remain inside the Runtime VM and must not be instrumented
# as Tool-sandbox operations.
TOOL_VM_TOOLS = ("exec", "process", "read", "write", "edit", "apply_patch")
RUNTIME_LOCAL_TOOLS = ("web_search", "web_fetch", "memory_search", "memory_get")


def openclaw_shared_ssh_runtime_directory(workspace_root: str) -> str:
    """Return the installed backend's deterministic shared runtime marker.

    OpenClaw's SSH backend treats an existing runtime marker as an already
    externalized workspace.  Creating it in the Tool bootstrap prevents the
    backend from copying the Runtime VM's local workspace over the Tool-owned
    mutable workspace.
    """
    value = 5381
    for character in "shared":
        value = ((value * 33) ^ ord(character)) & 0xFFFFFFFF
    return f"{workspace_root.rstrip('/')}/openclaw-ssh-shared-{value:x}"


@dataclass(frozen=True, slots=True)
class NativeSSHConfig:
    target: str
    identity_private_key: str
    host_public_key: str
    workspace_root: str = "/workspace"
    sandbox_id: str = ""
    host_key_alias: str = "openclaw-sandbox"


@dataclass(frozen=True, slots=True)
class NativeSSHRoute:
    """One admission-scoped raw TCP route owned by CubeSandbox."""

    sandbox_id: str
    container_port: int
    epoch: int
    host: str
    port: int

    def __post_init__(self) -> None:
        if self.epoch < 1:
            raise ValueError("native SSH route epoch must be a positive integer")
        if not 1 <= self.container_port <= 65535 or not 1 <= self.port <= 65535:
            raise ValueError("native SSH route has an invalid port")
        if not self.sandbox_id or not self.host:
            raise ValueError("native SSH route identity is empty")

    @property
    def target(self) -> str:
        return native_ssh_target(self.host, port=self.port)


def native_ssh_route(endpoint: Any, *, epoch: int) -> NativeSSHRoute:
    """Convert a semantic CubeSandbox endpoint for one SSH admission."""
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise ValueError("native SSH route epoch must be a positive integer")
    endpoint_id = str(endpoint.sandbox_id or "")
    container_port = int(endpoint.container_port)
    if not endpoint_id or not 1 <= container_port <= 65535:
        raise ValueError("CubeSandbox returned an invalid semantic TCP endpoint")
    # A semantic raw endpoint is already the complete host:port route returned
    # by CubeSandbox.  Do not let the generic OpenSSH target helper turn a
    # missing mapped port into the SSH default (22); that would make a broken
    # CubeSandbox response look like a valid route to another service.
    _user, host, port = split_native_ssh_target(
        str(endpoint.address or "").strip(), require_explicit_port=True,
    )
    return NativeSSHRoute(endpoint_id, container_port, epoch, host, port)


def native_ssh_host_key_alias(sandbox_id: str) -> str:
    """Return a stable, per-Tool host-key namespace."""
    value = f"clawbox-tool-{str(sandbox_id).strip()}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise ValueError("sandbox_id cannot produce a safe SSH host-key alias")
    return value


def split_native_ssh_target(
    target: str, *, default_port: int = 22, require_explicit_port: bool = False,
) -> tuple[str, str, int]:
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
    explicit_port = False
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
            explicit_port = True
        else:
            port = default_port
    elif address.count(":") == 1:
        host, rendered_port = address.rsplit(":", 1)
        if not host or not rendered_port.isdigit():
            raise ValueError(f"invalid native SSH target port: {target!r}")
        port = int(rendered_port)
        explicit_port = True
    else:
        host, port = address, default_port
    if require_explicit_port and not explicit_port:
        raise ValueError(
            f"raw CubeSandbox TCP endpoint must include a mapped port: {target!r}"
        )
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


def native_tool_bridge_setup_command(*, restart: bool = False) -> str:
    """Return the explicit post-create Tool SSH bootstrap command.

    Cube restores a template snapshot before applying per-sandbox environment
    variables, so the image entrypoint cannot reliably see ephemeral SSH keys.
    The setup phase therefore materializes those keys through envd and starts
    the native bridge before any Agent operation is admitted.
    """
    restart_command = ""
    if restart:
        restart_command = (
            "pkill -x tool-bridge 2>/dev/null || true; "
            "i=0; while grep -Eq ':08AE[[:space:]]' /proc/net/tcp && [ $i -lt 50 ]; do "
            "i=$((i + 1)); sleep 0.1; done; "
            "rm -f /run/clawtune/guest-collector.sock "
            "/run/clawtune/guest-collector.token "
            "/var/log/clawtune-guest-collector.unavailable; "
        )
    return (
        "set -eu; "
        "mkdir -p /etc/profile.d; "
        "printf 'export PYTHONHASHSEED=0\\n' > /etc/profile.d/clawbox-experiment.sh; "
        "mkdir -p /run/clawbox-ssh; "
        "printf '%s' \"$CLAWBOX_TOOL_HOST_KEY_B64\" | base64 -d > /run/clawbox-ssh/host_key; "
        "printf '%s' \"$CLAWBOX_TOOL_AUTHORIZED_KEY_B64\" | base64 -d > /run/clawbox-ssh/authorized_key; "
        "chmod 600 /run/clawbox-ssh/host_key /run/clawbox-ssh/authorized_key; "
        + restart_command
        + "kernel_source=/lib/modules/$(uname -r)/build; test -d \"$kernel_source\"; "
        + "if ! grep -Eq ':08AE[[:space:]]' /proc/net/tcp; then "
        "nohup env TOOL_BRIDGE_HOST_KEY=/run/clawbox-ssh/host_key "
        "TOOL_BRIDGE_AUTHORIZED_KEY=/run/clawbox-ssh/authorized_key "
        "TOOL_BRIDGE_LISTEN=0.0.0.0:2222 "
        "PYTHONHASHSEED=0 "
        "CLAWTUNE_GUEST_COLLECTOR_HELPER=/opt/clawtune-guest/tools/guest_collector_server.py "
        "CLAWTUNE_GUEST_COLLECTOR_PYTHON=/opt/clawtune/venv/bin/python "
        "PYTHONPATH=/opt/clawtune-guest/services/sidecar/src "
        "XDG_CACHE_HOME=/opt/clawtune/cache "
        "BCC_KERNEL_SOURCE=\"$kernel_source\" "
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
                 prediction_manifest: dict[str, dict[str, Any]] | None = None,
                 resident_poll: Callable[[str, float], CommandResult | None] | None = None,
                 checkpoint_relay: bool = False) -> dict:
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
    exec_yield_value = configuration.get("openclaw_exec_yield_ms")
    exec_yield_ms: int | None = None
    exec_yield_export = ""
    if exec_yield_value is not None:
        if isinstance(exec_yield_value, bool):
            raise ValueError("openclaw_exec_yield_ms must be an integer from 10 to 120000")
        try:
            exec_yield_ms = int(exec_yield_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "openclaw_exec_yield_ms must be an integer from 10 to 120000"
            ) from exc
        if str(exec_yield_ms) != str(exec_yield_value).strip() or not 10 <= exec_yield_ms <= 120000:
            raise ValueError("openclaw_exec_yield_ms must be an integer from 10 to 120000")
        exec_yield_export = f"OPENCLAW_BASH_YIELD_MS={exec_yield_ms} "
    if "api_key" in configuration:
        raise ValueError("OpenClaw API keys must come from an environment variable")
    if not ssh.sandbox_id:
        raise ValueError("native SSH requires the intended Tool sandbox_id")
    host_key_alias = ssh.host_key_alias.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", host_key_alias):
        raise ValueError("native SSH host_key_alias is not safe")

    home = f"/state/openclaw/{session_id}"
    # Record and replay use the same runtime setup and task instruction.
    runtime_workspace = "/workspace"
    openclaw_model = model
    trace_dir = f"/state/clawtune/{session_id}/traces"
    ssh_dir = f"{home}/ssh"
    identity_file = f"{ssh_dir}/id_ed25519"
    known_hosts_file = f"{ssh_dir}/known_hosts"
    launcher_dir = f"{home}/bin"
    ssh_launcher = f"{launcher_dir}/ssh"
    agent_pid_file = f"{home}/agent.pid"
    agent_stdout_file = f"{home}/logs/agent.stdout"
    agent_stderr_file = f"{home}/logs/agent.stderr"
    agent_exit_file = f"{home}/agent.exit"
    relay_script_file = f"{launcher_dir}/model-relay.py"
    relay_pid_file = f"{home}/model-relay.pid"
    prediction_file = f"/state/clawtune/{session_id}/runtime-predictions.json"
    prefix = (
        f"export HOME={shlex.quote(home)} OPENCLAW_HOME={shlex.quote(home + '/.openclaw')} "
        f"PATH={shlex.quote(launcher_dir)}:$PATH "
        f"CLAWBOX_POLICY_CONTROL_URL={shlex.quote(policy_control.url)} "
        # OpenClaw intentionally removes inherited *_TOKEN variables before
        # spawning its SSH backend. This per-session capability is not an LLM
        # provider credential; use a name that survives the installed
        # backend's environment sanitizer.
        f"CLAWTUNE_RUNTIME_ID={shlex.quote(session_id)} "
        f"CLAWTUNE_GATEWAY_ID={shlex.quote(session_id)} "
        f"CLAWBOX_POLICY_CONTROL_AUTH={shlex.quote(policy_control.token)} "
        f"CLAWBOX_POLICY_CONTROL_TOKEN={shlex.quote(policy_control.token)} "
        f"CLAWBOX_POLICY_SESSION_ID={shlex.quote(session_id)} "
        "CLAWBOX_POLICY_REQUIRE_ENVELOPE=1 "
        f"CLAWBOX_RUNTIME_PREDICTION_FILE={shlex.quote(prediction_file)} "
        f"CLAWBOX_TOOL_SANDBOX_ID={shlex.quote(ssh.sandbox_id)} "
        f"CLAWBOX_SSH_HOST_KEY_ALIAS={shlex.quote(host_key_alias)} "
        + exec_yield_export
        + f"CLAWTUNE_RUN_ID={shlex.quote(session_id)} CLAWTUNE_SESSION_ID={shlex.quote(session_id)}; "
    )

    def invoke(args: list[str], *, input_value: str | None = None,
               pid_file: str | None = None) -> CommandResult:
        if input_value is not None and pid_file is not None:
            raise ValueError("pid_file cannot be combined with stdin input")
        argv = " ".join(
            f'"${{{item.removeprefix("$ENV:")}}}"' if item.startswith("$ENV:")
            else shlex.quote(item) for item in [executable, *args]
        )
        if input_value is not None:
            encoded = base64.b64encode(input_value.encode()).decode()
            command = prefix + f"printf %s {shlex.quote(encoded)} | base64 -d | {argv}"
        elif pid_file is not None:
            command = prefix + f"printf '%s\\n' $$ > {shlex.quote(pid_file)}; exec {argv}"
        else:
            command = prefix + argv
        result = runtime_executor.execute(command, timeout_seconds)
        if result.exit_code:
            raise RuntimeError(f"OpenClaw Runtime VM command failed: {result.stderr[-2000:]}")
        return result

    private_b64 = base64.b64encode(ssh.identity_private_key.encode()).decode()
    _user, host, port = split_native_ssh_target(ssh.target)
    known_host = f"{host_key_alias} {ssh.host_public_key.strip()}\n"
    known_b64 = base64.b64encode(known_host.encode()).decode()
    launcher_b64 = base64.b64encode(
        b"#!/bin/sh\n"
        b"export CLAWBOX_POLICY_CONTROL_TOKEN="
        b"${CLAWBOX_POLICY_CONTROL_AUTH:-${CLAWBOX_POLICY_CONTROL_TOKEN:-}}\n"
        b"exec /usr/local/bin/ssh \"$@\"\n"
    ).decode()
    relay_b64 = base64.b64encode(RUNTIME_MODEL_RELAY_SCRIPT.encode()).decode()
    relay_setup = ""
    sidecar_upstream_url = upstream_url
    if checkpoint_relay:
        if model_gateway is None:
            raise ValueError("checkpoint model relay requires managed ModelGateway")
        sidecar_upstream_url = f"http://127.0.0.1:{RELAY_PORT}/v1"
        relay_setup = (
            f"printf %s {shlex.quote(relay_b64)} | base64 -d > "
            f"{shlex.quote(relay_script_file)}; "
            f"chmod 700 {shlex.quote(relay_script_file)}; "
            f"nohup env CLAWBOX_RELAY_UPSTREAM={shlex.quote(upstream_url)} "
            f"CLAWBOX_RELAY_TOKEN=\"${{{upstream_key_env}}}\" "
            f"CLAWBOX_RELAY_PORT={RELAY_PORT} "
            f"/opt/clawtune/venv/bin/python {shlex.quote(relay_script_file)} </dev/null "
            f">{shlex.quote(home + '/logs/model-relay.log')} 2>&1 & "
            f"echo $! >{shlex.quote(relay_pid_file)}; "
        )
    encoded_predictions = base64.b64encode(json.dumps(
        prediction_manifest or {}, sort_keys=True, separators=(",", ":"),
    ).encode()).decode()
    setup = runtime_executor.execute(
        prefix
        + f"mkdir -p {shlex.quote(runtime_workspace)} {shlex.quote(trace_dir + '/tool-resource')} "
        + f"{shlex.quote(launcher_dir)} "
        + f"{shlex.quote(home + '/logs')} {shlex.quote(ssh_dir)}; "
        + f"printf %s {shlex.quote(private_b64)} | base64 -d > {shlex.quote(identity_file)}; "
        + f"printf %s {shlex.quote(known_b64)} | base64 -d > {shlex.quote(known_hosts_file)}; "
        + f"printf %s {shlex.quote(launcher_b64)} | base64 -d > {shlex.quote(ssh_launcher)}; "
        + relay_setup
        + f"chmod 700 {shlex.quote(ssh_launcher)}; "
        + f"chmod 600 {shlex.quote(identity_file)} {shlex.quote(known_hosts_file)}; "
        + f"printf %s {shlex.quote(encoded_predictions)} | base64 -d > {shlex.quote(prediction_file)}; "
        + f"cp -n /opt/clawtune/cold-start/tool-resource/*-kb.json {shlex.quote(trace_dir + '/tool-resource')}/ 2>/dev/null || true; "
        + "export CLAWTUNE_POLICY=observe-only "
        + f"CLAWTUNE_TRACE_DIR={shlex.quote(trace_dir)} "
        + f"CLAWTUNE_TOOL_RESOURCE_ARTIFACT_DIR={shlex.quote(trace_dir + '/tool-resource')} "
        + "CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=false "
        + "CLAWTUNE_REPO_KEY=\"${CLAWBOX_REPO_KEY:-unknown}\" "
        + "CLAWTUNE_TRACE_MAX_MESSAGES_BYTES=67108864 "
        + f"CLAWTUNE_LLM_UPSTREAM_BASE_URL={shlex.quote(sidecar_upstream_url)} "
        + f"CLAWTUNE_LLM_UPSTREAM_API_KEY=\"${{{upstream_key_env}}}\" "
        + f"CLAWTUNE_LLM_PROXY_EXPOSE_MODEL={shlex.quote(openclaw_model)} "
        + f"CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL={shlex.quote(model)}; "
        + "nohup env XDG_CACHE_HOME=/opt/clawtune/cache "
        + "/opt/clawtune/venv/bin/python -m clawtune_sidecar.main "
        + f"--host 127.0.0.1 --port 8765 >{shlex.quote(home + '/logs/sidecar.log')} 2>&1 & "
        + f"echo $! >{shlex.quote(home + '/sidecar.pid')}", 30,
    )
    if setup.exit_code:
        raise RuntimeError(f"ClawTune sidecar setup failed: {setup.stderr[-2000:]}")
    relay_ready = (
        f"curl -fsS http://127.0.0.1:{RELAY_PORT}/healthz >/dev/null 2>&1 && "
        if checkpoint_relay else ""
    )
    ready = runtime_executor.execute(
        prefix + "for i in $(seq 1 120); do " + relay_ready
        + "curl -fsS http://127.0.0.1:8765/health/ready "
        + ">/dev/null 2>&1 && exit 0; sleep 0.5; done; exit 1", 70,
    )
    if ready.exit_code:
        raise RuntimeError("ClawTune sidecar did not become ready in the Runtime VM")
    invoke(["plugins", "install", "--link", clawtune_plugin])
    invoke(["plugins", "enable", "clawtune"])
    exec_tool_config: dict[str, object] = {
        "host": "sandbox", "security": "full", "ask": "off",
    }
    if exec_yield_ms is not None:
        # Set the OpenClaw configuration as well as the process environment.
        # The config is consumed directly by the exec tool factory and avoids
        # a load-dependent fallback to its 10-second default in long-lived or
        # restored Runtime processes.
        exec_tool_config["backgroundMs"] = exec_yield_ms
    patch = {
        "agents": {"defaults": {"workspace": runtime_workspace, "sandbox": {
            "mode": "all", "backend": "ssh", "scope": "shared", "workspaceAccess": "rw",
            "ssh": {"target": ssh.target,
                    "workspaceRoot": ssh.workspace_root,
                    "identityFile": identity_file, "knownHostsFile": known_hosts_file,
                    "strictHostKeyChecking": True, "updateHostKeys": False},
        }}},
        "tools": {
            "allow": [*TOOL_VM_TOOLS, *RUNTIME_LOCAL_TOOLS],
            "deny": ["browser", "canvas", "nodes", "cron", "gateway"],
            "exec": exec_tool_config,
            "elevated": {"enabled": False},
            "sandbox": {"tools": {
                "allow": list(TOOL_VM_TOOLS),
                "deny": ["browser", "canvas", "nodes", "cron", "gateway"],
            }},
        },
        "plugins": {"entries": {"clawtune": {"enabled": True, "config": {
            "endpoint": "http://127.0.0.1:8765", "mode": "observe", "failOpen": False,
            "executionBackend": "hook-only", "sandboxExecEnvelope": True,
            # OpenClaw resolves the configured sandbox backend after the
            # before_tool_call hook, so hook params without an explicit host
            # are reported as "gateway" even though execution is SSH-backed.
            # Tool-name filtering keeps Runtime-local web/memory tools out.
            "instrumentHosts": ["sandbox", "gateway"],
            "instrumentTools": list(TOOL_VM_TOOLS),
            "enableCgroup": False, "enableAffinity": False, "enableNuma": False,
            "autoStartSidecar": False, "securityBoundaryAccepted": True,
            "trace": {"schema_version": 6, "include_raw_events": True,
                      "include_llm_messages": True, "include_tool_outputs": True,
                      "redact_sensitive_data": False, "flush_span_start": True,
                      "max_messages_bytes": 67108864,
                      "max_string_bytes": 67108864,
                      "max_tool_output_bytes": 67108864,
                      "trace_dir": trace_dir},
        }}}},
    }
    invoke(["config", "patch", "--stdin"], input_value=json.dumps(patch))
    invoke(["onboard", "--non-interactive", "--accept-risk", "--skip-health", "--mode", "local",
            "--auth-choice", "vllm", "--custom-base-url", "http://127.0.0.1:8765/v1",
            "--custom-api-key", f"clawtune-runtime.{session_id}", "--custom-model-id", openclaw_model])
    # Preserve the request envelope used by the validated recording. Apply this
    # to live recording too, rather than keeping a replay-only configuration.
    invoke(["config", "unset", "models.providers.vllm.models.0.reasoning"])
    instruction = prompt
    agent_args = [
        "agent", "--local", "--agent", "main", "--session-id", session_id,
        "--model", f"vllm/{openclaw_model}", "--message", instruction,
        "--timeout", str(timeout_seconds), "--json",
    ]
    if resident_poll is None:
        result = invoke(agent_args, pid_file=agent_pid_file)
    else:
        argv = " ".join(shlex.quote(item) for item in [executable, *agent_args])
        body = (
            f"{argv}; status=$?; printf '%s\\n' \"$status\" > "
            f"{shlex.quote(agent_exit_file)}; exit \"$status\""
        )
        launched = runtime_executor.execute(
            prefix
            + f"rm -f {shlex.quote(agent_exit_file)} "
            + f"{shlex.quote(agent_stdout_file)} {shlex.quote(agent_stderr_file)}; "
            + f"nohup /bin/sh -c {shlex.quote(body)} </dev/null "
            + f">{shlex.quote(agent_stdout_file)} 2>{shlex.quote(agent_stderr_file)} & "
            + f"printf '%s\\n' $! > {shlex.quote(agent_pid_file)}",
            30,
        )
        if launched.exit_code:
            raise RuntimeError(
                f"OpenClaw Runtime VM launch failed: {launched.stderr[-2000:]}"
            )
        deadline = time.monotonic() + timeout_seconds
        while True:
            status = resident_poll(
                f"test -s {shlex.quote(agent_exit_file)}", 10,
            )
            if status is not None and status.exit_code == 0:
                break
            if time.monotonic() >= deadline:
                resident_poll(
                    f"kill -TERM $(cat {shlex.quote(agent_pid_file)}) 2>/dev/null || true",
                    10,
                )
                raise TimeoutError("OpenClaw detached Agent timed out")
            time.sleep(0.2)
        exit_result = resident_poll(f"cat {shlex.quote(agent_exit_file)}", 10)
        stdout_result = resident_poll(f"cat {shlex.quote(agent_stdout_file)}", 30)
        stderr_result = resident_poll(f"cat {shlex.quote(agent_stderr_file)}", 30)
        if exit_result is None or stdout_result is None or stderr_result is None:
            raise RuntimeError("Runtime became non-resident while collecting Agent result")
        try:
            exit_code = int(exit_result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError("OpenClaw detached Agent wrote an invalid exit status") from exc
        result = CommandResult(
            exit_code, stdout_result.stdout, stderr_result.stdout,
            max(0.0, timeout_seconds - max(0.0, deadline - time.monotonic())),
        )
        if result.exit_code:
            raise RuntimeError(f"OpenClaw Runtime VM command failed: {result.stderr[-2000:]}")
    host_home = output_dir / "openclaw" / session_id
    host_home.mkdir(parents=True, exist_ok=True)
    (host_home / "final-answer.json").write_text(result.stdout, encoding="utf-8")
    runtime_logs = {
        "agent.stderr.log": agent_stderr_file,
        "sidecar.log": home + "/logs/sidecar.log",
    }
    if checkpoint_relay:
        runtime_logs["model-relay.log"] = home + "/logs/model-relay.log"
    for local_name, remote_path in runtime_logs.items():
        log_result = runtime_executor.execute(
            prefix + f"cat {shlex.quote(remote_path)}", 30,
        )
        if log_result.exit_code == 0:
            (host_home / local_name).write_text(
                log_result.stdout, encoding="utf-8",
            )
    runtime_executor.execute(
        prefix + f"if [ -s {shlex.quote(home + '/sidecar.pid')} ]; then "
        + f"kill -TERM $(cat {shlex.quote(home + '/sidecar.pid')}) 2>/dev/null || true; fi; "
        + (f"if [ -s {shlex.quote(relay_pid_file)} ]; then "
           f"kill -TERM $(cat {shlex.quote(relay_pid_file)}) 2>/dev/null || true; fi"
           if checkpoint_relay else "true"), 10,
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
    agent_tool_completed = [
        item for item in completed
        if (item.get("request") or {}).get("execution_scope", "agent-tool") == "agent-tool"
    ]
    native_ssh_latencies = [
        max(0.0, float(item["completion"]["execution_completed_at"])
            - float(item["completion"]["execution_started_at"])) for item in completed
    ]
    latencies = [
        max(0.0, float(item["completion"]["execution_completed_at"])
            - float(item["completion"]["execution_started_at"]))
        for item in agent_tool_completed
    ]
    return {"stdout": result.stdout, "stderr": result.stderr,
            "tool_calls": len(agent_tool_completed),
            "native_ssh_executions": len(completed), "tool_latencies": latencies,
            "native_ssh_latencies": native_ssh_latencies,
            "agent_pid_file": agent_pid_file,
            "checkpoint_model_relay": checkpoint_relay,
            "runtime_traces": copied, "policy_control_records": control_records,
            "model_gateway_records": model_gateway.records() if model_gateway else [],
            "model_gateway_completeness": model_gateway.replay_completeness() if model_gateway else None}
