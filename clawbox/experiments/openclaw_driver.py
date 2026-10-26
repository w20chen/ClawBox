"""Run OpenClaw in a Runtime CubeSandbox with native SSH tools."""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import time
import urllib.parse
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
OPENCLAW_SSH_RUNTIME_ROOT = "/workspace"

# Initialize the session credential in the existing agent process through
# OpenClaw's exported API. Its native store owns locking and persistence; do
# not reproduce its SQLite schema or revive the legacy auth-profiles.json.
OPENCLAW_AUTH_PRELOAD = """\
import { realpathSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

if (process.env.CLAWBOX_OPENCLAW_NODE_OPTIONS_SET === 'x') {
  process.env.NODE_OPTIONS = process.env.CLAWBOX_OPENCLAW_NODE_OPTIONS;
} else {
  delete process.env.NODE_OPTIONS;
}
delete process.env.CLAWBOX_OPENCLAW_NODE_OPTIONS;
delete process.env.CLAWBOX_OPENCLAW_NODE_OPTIONS_SET;

const require = createRequire(realpathSync(process.argv[1]));
const { upsertAuthProfileWithLock } = await import(pathToFileURL(
  require.resolve('openclaw/plugin-sdk/provider-auth')).href);
const saved = await upsertAuthProfileWithLock({
  profileId: 'vllm:default',
  credential: {
    type: 'api_key', provider: 'vllm',
    key: 'clawtune-runtime.' + process.env.CLAWTUNE_RUNTIME_ID,
  },
  agentDir: join(process.env.OPENCLAW_STATE_DIR, 'agents', 'main', 'agent'),
});
if (!saved) throw new Error('OpenClaw native session credential initialization failed');
"""


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


def native_tool_workspace_setup_command(tool_workspace: str) -> str:
    """Initialize SSH in the image-owned workspace without copying the repo.

    Keep the agent, absolute replay paths and post-run validation on the same
    mutable tree. The marker prevents OpenClaw from copying Runtime files in.
    """
    source = str(tool_workspace or "").strip()
    if not source.startswith("/"):
        raise ValueError("Tool workspace must be an absolute POSIX path")
    runtime_dir = openclaw_shared_ssh_runtime_directory(source)
    return (
        f"set -eu; test -d {shlex.quote(source)}; "
        f"mkdir -p -- {shlex.quote(runtime_dir)}"
    )


def openclaw_real_ssh_path(session_id: str) -> str:
    """Path of OpenSSH preserved before the Runtime installs its policy shim."""
    return f"/state/openclaw/{session_id}/bin/ssh.real"


@dataclass(frozen=True, slots=True)
class NativeSSHConfig:
    target: str
    identity_private_key: str
    host_public_key: str
    workspace_root: str = OPENCLAW_SSH_RUNTIME_ROOT
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


def native_tool_bridge_setup_command(*, restart: bool = False,
                                     require_progress_events: bool = False,
                                     command_timeout_seconds: int = 300,
                                     apt_cache_proxy_url: str | None = None,
                                     pypi_cache_index_url: str | None = None) -> str:
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
            "i=0; while bridge_listening && [ $i -lt 50 ]; do "
            "i=$((i + 1)); sleep 0.1; done; "
            "if ! test -s /run/clawtune/prewarmed; then "
            "rm -f /run/clawtune/guest-collector.sock "
            "/run/clawtune/guest-collector.token "
            "/var/log/clawtune-guest-collector.unavailable; fi; "
        )
    progress_check = (
        "/usr/local/bin/tool-bridge --self-test | grep -q '\"progress_events\":true' || "
        "{ echo 'Toolbridge lacks execution progress events; rebuild the Tool image with clawbox experiment images' >&2; exit 1; }; "
    ) if require_progress_events else ""
    apt_proxy_setup = ""
    if apt_cache_proxy_url:
        directive = f'Acquire::http::Proxy "{apt_cache_proxy_url}";\n'
        apt_proxy_setup = (
            "mkdir -p /etc/apt/apt.conf.d; "
            f"printf %s {shlex.quote(directive)} "
            "> /etc/apt/apt.conf.d/01clawbox-package-cache; "
        )
    pypi_environment = ""
    if pypi_cache_index_url:
        pypi_host = urllib.parse.urlsplit(pypi_cache_index_url).hostname
        if not pypi_host:
            raise ValueError("PyPI cache index URL has no host")
        pypi_environment = (
            f"PIP_INDEX_URL={shlex.quote(pypi_cache_index_url)} "
            f"PIP_TRUSTED_HOST={shlex.quote(pypi_host)} "
        )
    return (
        "set -eu; "
        "bridge_listening() { awk '$2 ~ /:08AE$/ && $4 == \"0A\" "
        "{ found=1 } END { exit !found }' /proc/net/tcp; }; "
        + progress_check
        + apt_proxy_setup
        + "mkdir -p /etc/profile.d; "
        "printf 'export PYTHONHASHSEED=0\\n' > /etc/profile.d/clawbox-experiment.sh; "
        "mkdir -p /run/clawbox-ssh; "
        "printf '%s' \"$CLAWBOX_TOOL_HOST_KEY_B64\" | base64 -d > /run/clawbox-ssh/host_key; "
        "printf '%s' \"$CLAWBOX_TOOL_AUTHORIZED_KEY_B64\" | base64 -d > /run/clawbox-ssh/authorized_key; "
        "chmod 600 /run/clawbox-ssh/host_key /run/clawbox-ssh/authorized_key; "
        + restart_command
        + "kernel_source=/lib/modules/$(uname -r)/build; test -d \"$kernel_source\"; "
        + "zcat /proc/config.gz | cmp -s - \"$kernel_source/.config\" || "
        "{ echo 'Guest kernel/header configuration mismatch; rebuild with scripts/lab images' >&2; exit 1; }; "
        + "if ! bridge_listening; then "
        "nohup env TOOL_BRIDGE_HOST_KEY=/run/clawbox-ssh/host_key "
        "TOOL_BRIDGE_AUTHORIZED_KEY=/run/clawbox-ssh/authorized_key "
        "TOOL_BRIDGE_LISTEN=0.0.0.0:2222 "
        f"TOOL_EXEC_TIMEOUT_SECONDS={int(command_timeout_seconds)} "
        + pypi_environment
        + "PYTHONHASHSEED=0 "
        "CLAWTUNE_GUEST_COLLECTOR_HELPER=/opt/clawtune-guest/tools/guest_collector_server.py "
        "CLAWTUNE_GUEST_COLLECTOR_PYTHON=/opt/clawtune/venv/bin/python "
        "PYTHONPATH=/opt/clawtune-guest/services/sidecar/src "
        "XDG_CACHE_HOME=/opt/clawtune/cache "
        "BCC_KERNEL_SOURCE=\"$kernel_source\" "
        "/usr/local/bin/tool-bridge </dev/null >/var/log/tool-bridge.log 2>&1 & "
        "fi; "
        "ready=0; i=0; while [ $i -lt 400 ]; do "
        "if bridge_listening; then ready=1; break; fi; "
        "i=$((i + 1)); sleep 0.1; done; "
        "if [ $ready -ne 1 ] || ! test -S /run/clawtune/guest-collector.sock "
        "|| ! grep -q 'guest collector ready' /var/log/tool-bridge.log; then "
        "cat /var/log/tool-bridge.log >&2 || true; exit 1; fi"
    )


def run_openclaw(*, prompt: str, session_id: str, configuration: dict,
                 ssh: NativeSSHConfig, policy_control: Any,
                 runtime_executor: Any, output_dir: Path, timeout_seconds: int | None,
                 model_gateway: Any | None = None,
                 prediction_manifest: dict[str, dict[str, Any]] | None = None,
                 resident_poll: Callable[[str, float], CommandResult | None] | None = None,
                 resident_wait: Callable[[str, float], CommandResult | None] | None = None,
                 resident_file_nonempty: Callable[[str, float], bool | None] | None = None,
                 checkpoint_relay: bool = False,
                 startup_begin: Callable[[], None] | None = None,
                 startup_wait: Callable[[], None] | None = None,
                 startup_event: Callable[[dict[str, Any]], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> dict:
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
    transport_timeout_seconds = (
        None if timeout_seconds is None
        else min(86400, max(120, int(timeout_seconds) + 60))
    )
    tool_timeout_seconds = int(configuration.get("command_timeout_seconds") or 300)
    if tool_timeout_seconds < 1:
        raise ValueError("command_timeout_seconds must be positive")
    startup_value = configuration.get("runtime_startup_timeout_seconds", 60)
    if (isinstance(startup_value, bool) or not isinstance(startup_value, int)
            or not 1 <= startup_value <= 3600):
        raise ValueError("runtime_startup_timeout_seconds must be an integer from 1 to 3600")
    startup_timeout_seconds = startup_value
    completion_watch_seconds = configuration.get("runtime_completion_watch_seconds", 10)
    if (isinstance(completion_watch_seconds, bool)
            or not isinstance(completion_watch_seconds, (int, float))
            or not 1 <= completion_watch_seconds <= 60):
        raise ValueError("runtime_completion_watch_seconds must be between 1 and 60 seconds")
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
    # OPENCLAW_HOME is an effective home, not the state directory. Preserve
    # the state path used by the previous native onboard invocation.
    openclaw_state_dir = f"{home}/.openclaw/.openclaw"
    openclaw_config_file = f"{openclaw_state_dir}/openclaw.json"
    # Record and replay use the same runtime setup and task instruction.
    runtime_workspace = "/workspace"
    openclaw_model = model
    trace_dir = f"/state/clawtune/{session_id}/traces"
    ssh_dir = f"{home}/ssh"
    identity_file = f"{ssh_dir}/id_ed25519"
    known_hosts_file = f"{ssh_dir}/known_hosts"
    launcher_dir = f"{home}/bin"
    timed_sidecar_file = f"{launcher_dir}/timed-clawtune-sidecar.py"
    sidecar_timing_file = f"{home}/logs/sidecar-startup-timing.json"
    auth_preload_file = f"{launcher_dir}/openclaw-auth.mjs"
    ssh_launcher = f"{launcher_dir}/ssh"
    real_ssh = openclaw_real_ssh_path(session_id)
    agent_pid_file = f"{home}/agent.pid"
    agent_stdout_file = f"{home}/logs/agent.stdout"
    agent_stderr_file = f"{home}/logs/agent.stderr"
    agent_exit_file = f"{home}/completion/agent.exit"
    policy_audit_file = f"{home}/logs/policy-ssh.jsonl"
    relay_script_file = f"{launcher_dir}/model-relay.py"
    relay_pid_file = f"{home}/model-relay.pid"
    prediction_file = f"/state/clawtune/{session_id}/runtime-predictions.json"
    prefix = (
        f"export HOME={shlex.quote(home)} OPENCLAW_HOME={shlex.quote(home + '/.openclaw')} "
        f"OPENCLAW_STATE_DIR={shlex.quote(openclaw_state_dir)} "
        f"OPENCLAW_CONFIG_PATH={shlex.quote(openclaw_config_file)} "
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
        f"CLAWBOX_POLICY_AUDIT_FILE={shlex.quote(policy_audit_file)} "
        f"CLAWBOX_SSH_CONTROL_DIR={shlex.quote(ssh_dir)} "
        f"CLAWBOX_TOOL_TIMEOUT_SECONDS={tool_timeout_seconds} "
        f"CLAWBOX_REAL_SSH={shlex.quote(real_ssh)} "
        "CLAWBOX_POLICY_REQUIRE_ENVELOPE=1 "
        f"CLAWBOX_RUNTIME_PREDICTION_FILE={shlex.quote(prediction_file)} "
        f"CLAWBOX_TOOL_SANDBOX_ID={shlex.quote(ssh.sandbox_id)} "
        f"CLAWBOX_SSH_HOST_KEY_ALIAS={shlex.quote(host_key_alias)} "
        + exec_yield_export
        + f"CLAWTUNE_RUN_ID={shlex.quote(session_id)} CLAWTUNE_SESSION_ID={shlex.quote(session_id)}; "
    )
    # Apply --import only to the native agent launch. The preload restores
    # inherited NODE_OPTIONS before OpenClaw can spawn any child processes.
    agent_prefix = prefix + (
        'export CLAWBOX_OPENCLAW_NODE_OPTIONS_SET="${NODE_OPTIONS+x}" '
        'CLAWBOX_OPENCLAW_NODE_OPTIONS="${NODE_OPTIONS-}"; '
        'export NODE_OPTIONS="${NODE_OPTIONS-} --import='
        + urllib.parse.quote(auth_preload_file, safe="/") + '"; '
    )

    def emit_startup_stage(
        stage: str, started: float, *, status: str = "ok",
        details: dict[str, Any] | None = None,
    ) -> None:
        if startup_event is not None:
            startup_event({
                "event": "openclaw_startup_stage",
                "session_id": session_id,
                "stage": stage,
                "status": status,
                "duration_seconds": max(0.0, time.monotonic() - started),
                **({"guest_timing": details} if details is not None else {}),
            })

    def invoke(args: list[str], *, input_value: str | None = None,
               pid_file: str | None = None,
               startup_stage: str | None = None) -> CommandResult:
        if input_value is not None and pid_file is not None:
            raise ValueError("pid_file cannot be combined with stdin input")
        argv = " ".join(
            f'"${{{item.removeprefix("$ENV:")}}}"' if item.startswith("$ENV:")
            else shlex.quote(item) for item in [executable, *args]
        )
        if input_value is not None:
            encoded = base64.b64encode(input_value.encode()).decode()
            command = agent_prefix + f"printf %s {shlex.quote(encoded)} | base64 -d | {argv}"
        elif pid_file is not None:
            command = agent_prefix + f"printf '%s\\n' $$ > {shlex.quote(pid_file)}; exec {argv}"
        else:
            command = agent_prefix + argv
        started = time.monotonic()
        try:
            result = runtime_executor.execute(command, timeout_seconds or 300)
        except BaseException:
            if startup_stage is not None:
                emit_startup_stage(startup_stage, started, status="error")
            raise
        if startup_stage is not None:
            emit_startup_stage(
                startup_stage, started,
                status="ok" if result.exit_code == 0 else "error",
            )
        if result.exit_code:
            raise RuntimeError(f"OpenClaw Runtime VM command failed: {result.stderr[-2000:]}")
        return result

    private_b64 = base64.b64encode(ssh.identity_private_key.encode()).decode()
    # Older Runtime images do not carry the ClawTune state initializer at
    # /usr/local/bin.  Ship the versioned helper with the Runtime setup so
    # experiments remain self-contained while still using ClawTune's native
    # StateStore/initialize_state implementation.
    init_state_file = f"{home}/initialize-clawtune-state.py"
    init_state_source = Path(__file__).resolve().parents[2] / "scripts" / "initialize-clawtune-state.py"
    init_state_b64 = base64.b64encode(init_state_source.read_bytes()).decode()
    timed_sidecar_source = Path(__file__).resolve().parents[2] / "scripts" / "timed-clawtune-sidecar.py"
    timed_sidecar_b64 = base64.b64encode(
        timed_sidecar_source.read_text(encoding="utf-8").replace("\r\n", "\n").encode()
    ).decode()
    _user, host, port = split_native_ssh_target(ssh.target)
    known_host = f"{host_key_alias} {ssh.host_public_key.strip()}\n"
    known_b64 = base64.b64encode(known_host.encode()).decode()
    # Ship the policy shim used by this worker. Runtime images are reusable;
    # their baked /usr/local/bin/ssh may implement an older request schema.
    policy_ssh_source = (
        Path(__file__).resolve().parents[2] / "scripts" / "clawbox-policy-ssh.py"
    )
    # This repository may be checked out with CRLF on the control host. The
    # launcher runs through its shebang in a Linux Runtime VM, so normalize
    # the per-session copy while packaging it.
    launcher_source = policy_ssh_source.read_text(encoding="utf-8").replace(
        "\r\n", "\n"
    ).encode()
    launcher_b64 = base64.b64encode(launcher_source).decode()
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
            f"CLAWBOX_RELAY_TIMEOUT_SECONDS={transport_timeout_seconds} "
            f"/opt/clawtune/venv/bin/python {shlex.quote(relay_script_file)} </dev/null "
            f">{shlex.quote(home + '/logs/model-relay.log')} 2>&1 & "
            f"echo $! >{shlex.quote(relay_pid_file)}; "
        )
    encoded_predictions = base64.b64encode(json.dumps(
        prediction_manifest or {}, sort_keys=True, separators=(",", ":"),
    ).encode()).decode()
    setup_started = time.monotonic()
    try:
        setup = runtime_executor.execute(
            prefix
        + f"mkdir -p {shlex.quote(runtime_workspace)} {shlex.quote(trace_dir + '/tool-resource')} "
        + f"{shlex.quote(launcher_dir)} "
        + f"{shlex.quote(home + '/logs')} {shlex.quote(ssh_dir)}; "
        + f"printf %s {shlex.quote(private_b64)} | base64 -d > {shlex.quote(identity_file)}; "
        + f"printf %s {shlex.quote(known_b64)} | base64 -d > {shlex.quote(known_hosts_file)}; "
        + f"printf %s {shlex.quote(launcher_b64)} | base64 -d > {shlex.quote(ssh_launcher)}; "
        # OpenClaw uses two SSH paths: backend preparation resolves
        # /usr/local/bin/ssh, while Agent exec calls resolve /usr/bin/ssh.
        # Preserve the real client under this session before replacing both
        # entry points. Otherwise only the first call reaches admission and a
        # concurrent/full replay silently bypasses the policy controller.
        + f"install -m 0755 /usr/bin/ssh {shlex.quote(real_ssh)}; "
        + f"install -m 0755 {shlex.quote(ssh_launcher)} /usr/local/bin/ssh; "
        + f"install -m 0755 {shlex.quote(ssh_launcher)} /usr/bin/ssh; "
        + f"printf %s {shlex.quote(init_state_b64)} | base64 -d > {shlex.quote(init_state_file)}; "
        + f"printf %s {shlex.quote(timed_sidecar_b64)} | base64 -d > {shlex.quote(timed_sidecar_file)}; "
        + f"test -d {shlex.quote(clawtune_plugin)}; "
        + relay_setup
        + f"chmod 700 {shlex.quote(ssh_launcher)} {shlex.quote(init_state_file)} {shlex.quote(timed_sidecar_file)}; "
        + f"chmod 700 {shlex.quote(ssh_dir)}; "
        + f"chmod 600 {shlex.quote(identity_file)} {shlex.quote(known_hosts_file)}; "
        + f"printf %s {shlex.quote(encoded_predictions)} | base64 -d > {shlex.quote(prediction_file)}; "
        + f"/opt/clawtune/venv/bin/python {shlex.quote(init_state_file)} --state {shlex.quote(trace_dir + '/tool-resource')} --owner {shlex.quote(session_id)} || exit $?; "
        + "export CLAWTUNE_POLICY=observe-only "
        + f"CLAWTUNE_TRACE_DIR={shlex.quote(trace_dir)} "
        + f"CLAWTUNE_TOOL_RESOURCE_ARTIFACT_DIR={shlex.quote(trace_dir + '/tool-resource')} "
        + "CLAWTUNE_TOOL_RESOURCE_EBPF_ENABLED=false "
        + "CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=false "
        + "CLAWTUNE_TOOL_RESOURCE_MEMORY_MEASUREMENT=cgroup_v2_environment_union_v1 "
        + "CLAWTUNE_REPO_KEY=\"${CLAWBOX_REPO_KEY:-unknown}\" "
        + "CLAWTUNE_TRACE_MAX_MESSAGES_BYTES=67108864 "
        + f"CLAWTUNE_LLM_UPSTREAM_BASE_URL={shlex.quote(sidecar_upstream_url)} "
        + f"CLAWTUNE_LLM_UPSTREAM_API_KEY=\"${{{upstream_key_env}}}\" "
        + f"CLAWTUNE_LLM_PROXY_EXPOSE_MODEL={shlex.quote(openclaw_model)} "
        + f"CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL={shlex.quote(model)}; "
        + "nohup env XDG_CACHE_HOME=/opt/clawtune/cache "
        # Cube command processes do not inherit Dockerfile ENV values from
        # the template entrypoint. Point the sidecar at the immutable seed
        # explicitly so it can use the prepared image-layer cache.
        + "CLAWTUNE_KB_SEED=/opt/clawtune/cold-start/tool-resource "
        + f"/opt/clawtune/venv/bin/python {shlex.quote(timed_sidecar_file)} "
        + f"--host 127.0.0.1 --port 8765 --timing-output {shlex.quote(sidecar_timing_file)} "
        + f">{shlex.quote(home + '/logs/sidecar.log')} 2>&1 & "
        + f"echo $! >{shlex.quote(home + '/sidecar.pid')}", 30,
        )
    except BaseException:
        emit_startup_stage("runtime_setup", setup_started, status="error")
        raise
    emit_startup_stage(
        "runtime_setup", setup_started,
        status="ok" if setup.exit_code == 0 else "error",
    )
    if setup.exit_code:
        raise RuntimeError(f"ClawTune sidecar setup failed: {setup.stderr[-2000:]}")
    if startup_begin is not None:
        startup_begin()
    relay_ready = (
        f"curl --max-time 2 -fsS http://127.0.0.1:{RELAY_PORT}/healthz >/dev/null 2>&1 && "
        if checkpoint_relay else ""
    )
    ready_started = time.monotonic()
    try:
        ready = runtime_executor.execute(
            prefix + f"deadline=$(( $(date +%s) + {startup_timeout_seconds} )); "
            + "while [ $(date +%s) -lt $deadline ]; do " + relay_ready
            + "if curl --max-time 2 -fsS http://127.0.0.1:8765/health/ready "
            + f">/dev/null 2>&1; then cat {shlex.quote(sidecar_timing_file)}; exit 0; fi; "
            + "sleep 0.5; done; exit 1",
            startup_timeout_seconds + 10,
        )
        if ready.exit_code:
            raise RuntimeError("ClawTune sidecar did not become ready in the Runtime VM")
    except Exception:
        emit_startup_stage("sidecar_ready", ready_started, status="error")
        failure_home = output_dir / "openclaw" / session_id
        failure_home.mkdir(parents=True, exist_ok=True)
        # Keep the original startup failure even when the guest is too unhealthy
        # to return its log. Capture this before session cleanup destroys the VM.
        try:
            diagnostic = runtime_executor.execute(
                prefix + f"tail -c 65536 {shlex.quote(home + '/logs/sidecar.log')}", 10,
            )
            (failure_home / "sidecar-startup.log").write_text(
                diagnostic.stdout + diagnostic.stderr, encoding="utf-8",
            )
        except Exception as diagnostic_error:
            (failure_home / "sidecar-startup-unavailable.txt").write_text(
                str(diagnostic_error), encoding="utf-8",
            )
        raise
    guest_timing = None
    try:
        guest_timing = json.loads(ready.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        pass
    emit_startup_stage("sidecar_ready", ready_started, details=guest_timing)
    exec_tool_config: dict[str, object] = {
        "host": "sandbox", "security": "full", "ask": "off",
    }
    if transport_timeout_seconds is not None:
        # OpenClaw's timeout covers only the transport wrapper.  The wrapper
        # starts the user-visible Tool deadline after policy admission.
        exec_tool_config["timeoutSec"] = transport_timeout_seconds
    if exec_yield_ms is not None:
        # Set the OpenClaw configuration as well as the process environment.
        # The config is consumed directly by the exec tool factory and avoids
        # a load-dependent fallback to its 10-second default in long-lived or
        # restored Runtime processes.
        exec_tool_config["backgroundMs"] = exec_yield_ms
    provider_patch: dict[str, object] = {
        "baseUrl": "http://127.0.0.1:8765/v1",
        "api": "openai-completions",
        "apiKey": "VLLM_API_KEY",
        # Preserve the validated recording's request envelope: no reasoning
        # field, matching the previous patch after native onboarding.
        "models": [{
            "id": openclaw_model,
            "name": openclaw_model,
            "input": ["text"],
            "cost": {
                "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
            },
            "contextWindow": 128000,
            "maxTokens": 8192,
        }],
    }
    if transport_timeout_seconds is not None:
        # Admission control can intentionally hold an already-issued model
        # request while earlier Tools release their memory reservations.
        provider_patch["timeoutSeconds"] = transport_timeout_seconds
    native_config = {
        "auth": {"profiles": {"vllm:default": {"provider": "vllm", "mode": "api_key"}}},
        "session": {"dmScope": "per-channel-peer"},
        "gateway": {
            "mode": "local", "port": 18789, "bind": "loopback",
            "auth": {"mode": "token", "token": secrets.token_hex(24)},
            "tailscale": {"mode": "off", "resetOnExit": False},
        },
        "skills": {"install": {"nodeManager": "npm"}},
        "hooks": {"internal": {"entries": {"session-memory": {"enabled": True}}}},
        "agents": {"defaults": {"workspace": runtime_workspace,
            "model": {"primary": f"vllm/{openclaw_model}"}, "sandbox": {
            "mode": "all", "backend": "ssh", "scope": "shared", "workspaceAccess": "rw",
            "ssh": {"target": ssh.target,
                    "workspaceRoot": ssh.workspace_root,
                    "identityFile": identity_file, "knownHostsFile": known_hosts_file,
                    "strictHostKeyChecking": True, "updateHostKeys": False},
        }}},
        "tools": {
            "profile": "coding",
            "allow": [*TOOL_VM_TOOLS, *RUNTIME_LOCAL_TOOLS],
            "deny": ["browser", "canvas", "nodes", "cron", "gateway"],
            "exec": exec_tool_config,
            "elevated": {"enabled": False},
            "sandbox": {"tools": {
                "allow": list(TOOL_VM_TOOLS),
                "deny": ["browser", "canvas", "nodes", "cron", "gateway"],
            }},
        },
        "models": {"mode": "merge", "providers": {"vllm": provider_patch}},
        "plugins": {
            "load": {"paths": [clawtune_plugin]},
            "entries": {"vllm": {"enabled": True}, "clawtune": {"enabled": True, "config": {
            "endpoint": "http://127.0.0.1:8765", "mode": "observe", "failOpen": False,
            "executionBackend": "hook-only", "sandboxExecEnvelope": True,
            "sandboxExecPredictionModel": "lattice",
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
    config_b64 = base64.b64encode(json.dumps(native_config).encode()).decode()
    auth_preload_b64 = base64.b64encode(OPENCLAW_AUTH_PRELOAD.encode()).decode()
    config_started = time.monotonic()
    try:
        configured = runtime_executor.execute(
            prefix + "set -eu; umask 077; "
            + f"mkdir -p {shlex.quote(openclaw_state_dir)}; "
            + f"printf %s {shlex.quote(config_b64)} | base64 -d > "
            + f"{shlex.quote(openclaw_config_file)}; "
            + f"printf %s {shlex.quote(auth_preload_b64)} | base64 -d > "
            + shlex.quote(auth_preload_file),
            30,
        )
        if configured.exit_code:
            raise RuntimeError(
                "OpenClaw native config setup failed: " + configured.stderr[-2000:]
            )
    except BaseException:
        emit_startup_stage("openclaw_config", config_started, status="error")
        raise
    emit_startup_stage("openclaw_config", config_started)
    if startup_wait is not None:
        startup_wait()
    if startup_event is not None:
        startup_event({
            "event": "agent_runnable",
            "session_id": session_id,
            "stage": "agent_runnable",
            "status": "ok",
        })
    instruction = prompt
    agent_args = [
        "agent", "--local", "--agent", "main", "--session-id", session_id,
        "--model", f"vllm/{openclaw_model}", "--message", instruction,
        "--timeout", str(timeout_seconds if timeout_seconds is not None else 0), "--json",
    ]
    if resident_poll is None:
        if timeout_seconds is None:
            raise ValueError("unbounded agent execution requires detached Runtime polling")
        result = invoke(agent_args, pid_file=agent_pid_file)
        final_response_observed_unix_s = time.time()
        final_response_observed_monotonic_s = time.monotonic()
    else:
        argv = " ".join(shlex.quote(item) for item in [executable, *agent_args])
        body = (
            f"{argv}; status=$?; printf '%s\\n' \"$status\" > "
            f"{shlex.quote(agent_exit_file)}; exit \"$status\""
        )
        launched = runtime_executor.execute(
            agent_prefix
            + f"mkdir -p {shlex.quote(home + '/completion')}; "
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
        agent_started = time.monotonic()
        deadline = None if timeout_seconds is None else agent_started + timeout_seconds
        if resident_wait is not None and timeout_seconds is not None:
            try:
                status = resident_wait(agent_exit_file, float(timeout_seconds))
            except TimeoutError:
                resident_poll(
                    f"kill -TERM $(cat {shlex.quote(agent_pid_file)}) 2>/dev/null || true",
                    10,
                )
                raise TimeoutError("OpenClaw detached Agent timed out")
            if status is None:
                raise RuntimeError("Runtime became non-resident while waiting for Agent result")
            if status.exit_code != 0:
                raise RuntimeError(
                    "resident Agent completion wait failed: "
                    + (status.stderr or status.stdout)[-1000:]
                )
        else:
            while True:
                if cancelled is not None and cancelled():
                    resident_poll(
                        f"kill -TERM $(cat {shlex.quote(agent_pid_file)}) 2>/dev/null || true",
                        10,
                    )
                    raise InterruptedError("OpenClaw Agent cancelled after arm failure")
                if resident_file_nonempty is not None:
                    watch_seconds = float(completion_watch_seconds)
                    if deadline is not None:
                        watch_seconds = max(0.001, min(watch_seconds, deadline-time.monotonic()))
                    ready = resident_file_nonempty(agent_exit_file, watch_seconds)
                else:
                    status = resident_poll(
                        f"test -s {shlex.quote(agent_exit_file)}", 10,
                    )
                    ready = status is not None and status.exit_code == 0
                if ready:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    resident_poll(
                        f"kill -TERM $(cat {shlex.quote(agent_pid_file)}) 2>/dev/null || true",
                        10,
                    )
                    raise TimeoutError("OpenClaw detached Agent timed out")
                time.sleep(1.0)
        exit_result = resident_poll(f"cat {shlex.quote(agent_exit_file)}", 10)
        stdout_result = resident_poll(f"cat {shlex.quote(agent_stdout_file)}", 30)
        # Host receipt of the completed Agent output, before diagnostic/artifact
        # collection. This includes completion polling and output transport.
        final_response_observed_unix_s = time.time()
        final_response_observed_monotonic_s = time.monotonic()
        stderr_result = resident_poll(f"cat {shlex.quote(agent_stderr_file)}", 30)
        if exit_result is None or stdout_result is None or stderr_result is None:
            raise RuntimeError("Runtime became non-resident while collecting Agent result")
        try:
            exit_code = int(exit_result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError("OpenClaw detached Agent wrote an invalid exit status") from exc
        result = CommandResult(
            exit_code, stdout_result.stdout, stderr_result.stdout,
            max(0.0, time.monotonic() - agent_started),
        )
        if result.exit_code:
            # Preserve the complete Agent streams before the owning VM is
            # destroyed.  Tail-only errors routinely hide the first fatal
            # line behind verbose ClawTune prediction diagnostics.
            failure_home = output_dir / "openclaw" / session_id
            failure_home.mkdir(parents=True, exist_ok=True)
            (failure_home / "agent.stdout.log").write_text(
                result.stdout, encoding="utf-8",
            )
            (failure_home / "agent.stderr.log").write_text(
                result.stderr, encoding="utf-8",
            )
            for local_name, remote_path in {
                "policy-ssh.jsonl": policy_audit_file,
                "sidecar.log": home + "/logs/sidecar.log",
            }.items():
                diagnostic = runtime_executor.execute(
                    prefix + f"cat {shlex.quote(remote_path)}", 30,
                )
                if diagnostic.exit_code == 0:
                    (failure_home / local_name).write_text(
                        diagnostic.stdout, encoding="utf-8",
                    )
            (failure_home / "policy-control.json").write_text(
                json.dumps(policy_control.records(), indent=2), encoding="utf-8",
            )
            excerpt = result.stderr[:2000]
            if len(result.stderr) > 4000:
                excerpt += "\n...[stderr middle omitted; full log preserved]...\n"
            if len(result.stderr) > 2000:
                excerpt += result.stderr[-2000:]
            raise RuntimeError(
                "OpenClaw Runtime VM command failed "
                f"(exit {result.exit_code}; full log {failure_home / 'agent.stderr.log'}): "
                + excerpt
            )
    if startup_event is not None:
        startup_event({
            "event": "final_response_observed", "session_id": session_id,
            "observed_unix_s": final_response_observed_unix_s,
            "observed_monotonic_s": final_response_observed_monotonic_s,
            "endpoint": "completed_agent_stdout_received_by_host",
            "includes_completion_polling_and_transport": True,
        })
    host_home = output_dir / "openclaw" / session_id
    host_home.mkdir(parents=True, exist_ok=True)
    (host_home / "final-answer.json").write_text(result.stdout, encoding="utf-8")
    try:
        final_payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        final_payload = {}
    final_meta = final_payload.get("meta") or {}
    replay_invalid = final_meta.get("replayInvalid") is True
    tool_failures = (final_meta.get("toolSummary") or {}).get("failures")
    if isinstance(tool_failures, bool) or not isinstance(tool_failures, int):
        tool_failures = None
    runtime_logs = {
        "agent.stderr.log": agent_stderr_file,
        "sidecar.log": home + "/logs/sidecar.log",
        "policy-ssh.jsonl": policy_audit_file,
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
            "final_response_observed_unix_s": final_response_observed_unix_s,
            "final_response_observed_monotonic_s": final_response_observed_monotonic_s,
            "tool_calls": len(agent_tool_completed),
            "native_ssh_executions": len(completed), "tool_latencies": latencies,
            "native_ssh_latencies": native_ssh_latencies,
            "agent_pid_file": agent_pid_file,
            "checkpoint_model_relay": checkpoint_relay,
            "runtime_traces": copied, "policy_control_records": control_records,
            "model_gateway_records": model_gateway.records() if model_gateway else [],
            "model_gateway_completeness": model_gateway.replay_completeness() if model_gateway else None,
            "agent_replay_invalid": replay_invalid,
            "agent_tool_failures": tool_failures}
