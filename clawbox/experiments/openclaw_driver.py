"""Trusted OpenClaw runner with a loopback-only CubeSandbox tool bridge."""
from __future__ import annotations

import json
import os
import re
import secrets
import base64
import shlex
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from clawbox.replay.lifecycle import CommandResult


_EXECUTION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CubeToolBridge:
    """Expose one authenticated session executor to its Runtime VM."""

    def __init__(self, execute: Callable[[str, float, str], CommandResult], *,
                 advertise_host: str | None = None) -> None:
        self._execute = execute
        self.token = secrets.token_urlsafe(32)
        self.calls: list[CommandResult] = []
        self.requests: list[dict[str, Any]] = []
        self.bind_host = "0.0.0.0" if advertise_host or os.environ.get("CLAWBOX_BRIDGE_HOST") else "127.0.0.1"
        self.advertised_host = advertise_host or os.environ.get("CLAWBOX_BRIDGE_HOST") or "127.0.0.1"
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                bridge.requests.append({
                    "source_ip": self.client_address[0],
                    "source_port": self.client_address[1],
                    "path": self.path,
                })
                if self.path != "/execute":
                    self.send_error(404)
                    return
                supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if not secrets.compare_digest(supplied, bridge.token):
                    self.send_error(401)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 1_048_576:
                        raise ValueError("invalid request size")
                    body = json.loads(self.rfile.read(length))
                    command = str(body["command"])
                    execution_id = str(body["execution_id"])
                    if not _EXECUTION_ID.fullmatch(execution_id):
                        raise ValueError("invalid execution_id")
                    timeout = min(3600.0, max(1.0, float(body.get("timeout_seconds") or 300)))
                    result = bridge._execute(command, timeout, execution_id)
                    bridge.calls.append(result)
                    payload = {
                        "exit_code": result.exit_code, "stdout": result.stdout,
                        "stderr": result.stderr, "duration_seconds": result.duration_s,
                        "execution_id": execution_id,
                    }
                    self._json(200, payload)
                except Exception as exc:
                    self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

            def _json(self, status: int, value: dict) -> None:
                encoded = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer((self.bind_host, 0), Handler)
        self.actual_port = int(self.server.server_port)
        self.startup = {
            "bind_host": self.bind_host,
            "advertised_host": self.advertised_host,
            "actual_port": self.actual_port,
            "url": self.url,
        }
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="cube-tool-bridge", daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.advertised_host}:{self.actual_port}"

    def __enter__(self) -> "CubeToolBridge":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def run_openclaw(*, prompt: str, session_id: str, configuration: dict,
                 bridge: CubeToolBridge, runtime_executor: Any,
                 output_dir: Path, timeout_seconds: int) -> dict:
    """Run OpenClaw + ClawTune inside the agent's Runtime CubeSandbox."""
    executable = str(configuration.get("openclaw_bin") or "openclaw")
    plugin = "/opt/clawbox/openclaw-plugins/cube-tool"
    clawtune_plugin = "/opt/clawtune/packages/clawtune-plugin"
    base_url = str(configuration.get("base_url") or os.environ.get("OPENCLAW_BASE_URL", ""))
    model = str(configuration.get("model") or os.environ.get("OPENCLAW_MODEL_REF", ""))
    key_env = str(configuration.get("api_key_env", "OPENCLAW_API_KEY"))
    if not _ENV_NAME.fullmatch(key_env):
        raise ValueError("api_key_env must be a valid environment variable name")
    api_key = os.environ.get(key_env, "")
    if not prompt.strip():
        raise ValueError("OpenClaw workload case requires a non-empty prompt")
    if not base_url or not model or not api_key:
        raise ValueError(f"OpenClaw requires base_url, model, and credential environment {key_env}")
    if "api_key" in configuration:
        raise ValueError("OpenClaw API keys must come from a Kubernetes Secret environment variable")

    home = f"/state/openclaw/{session_id}"
    workspace = f"{home}/runtime-workspace"
    trace_dir = f"/state/clawtune/{session_id}/traces"
    prefix = (
        f"export HOME={shlex.quote(home)} OPENCLAW_HOME={shlex.quote(home + '/.openclaw')} "
        f"CLAWBOX_CUBE_TOOL_URL={shlex.quote(bridge.url)} "
        f"CLAWBOX_CUBE_TOOL_TOKEN={shlex.quote(bridge.token)} "
        f"CLAWTUNE_RUN_ID={shlex.quote(session_id)} CLAWTUNE_SESSION_ID={shlex.quote(session_id)}; "
    )

    def invoke(args: list[str], *, input_value: str | None = None) -> CommandResult:
        argv = " ".join(
            f'"${{{item.removeprefix("$ENV:")}}}"' if item.startswith("$ENV:")
            else shlex.quote(item)
            for item in [executable, *args]
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

    setup = runtime_executor.execute(
        prefix
        + f"mkdir -p {shlex.quote(workspace)} {shlex.quote(trace_dir + '/tool-resource')} "
        + f"{shlex.quote(home + '/logs')}; "
        + f"cp -n /opt/clawtune/cold-start/tool-resource/*-kb.json "
        + f"{shlex.quote(trace_dir + '/tool-resource')}/ 2>/dev/null || true; "
        + "if [ -n \"${CLAWBOX_KB_ENDPOINT:-}\" ] && "
        + "[ -n \"${CLAWBOX_KB_TOKEN:-}\" ]; then "
        + "/opt/clawtune/venv/bin/python /usr/local/bin/native-kb-pull.py "
        + "--endpoint \"$CLAWBOX_KB_ENDPOINT\" --token \"$CLAWBOX_KB_TOKEN\" "
        + "--tenant \"${CLAWBOX_TENANT_ID:-default}\" "
        + "--repo \"${CLAWBOX_REPO_KEY:-unknown}\" "
        + f"--artifact-dir {shlex.quote(trace_dir + '/tool-resource')} "
        + f">{shlex.quote(home + '/logs/kb-pull.log')} 2>&1 || "
        + f"printf '%s\\n' 'control-plane KB pull unavailable; cold-start retained' "
        + f">{shlex.quote(home + '/logs/kb-pull.unavailable')}; fi; "
        + "export CLAWTUNE_POLICY=observe-only "
        + f"CLAWTUNE_TRACE_DIR={shlex.quote(trace_dir)} "
        + f"CLAWTUNE_TOOL_RESOURCE_ARTIFACT_DIR={shlex.quote(trace_dir + '/tool-resource')} "
        + "CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=false "
        + "CLAWTUNE_REPO_KEY=\"${CLAWBOX_REPO_KEY:-unknown}\" "
        + f"CLAWTUNE_LLM_UPSTREAM_BASE_URL={shlex.quote(base_url)} "
        + f"CLAWTUNE_LLM_UPSTREAM_API_KEY=\"${{{key_env}}}\" "
        + f"CLAWTUNE_LLM_PROXY_EXPOSE_MODEL={shlex.quote(model)} "
        + f"CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL={shlex.quote(model)}; "
        + "nohup /opt/clawtune/venv/bin/python -m clawtune_sidecar.main "
        + f"--host 127.0.0.1 --port 8765 >{shlex.quote(home + '/logs/sidecar.log')} 2>&1 & "
        + f"echo $! >{shlex.quote(home + '/sidecar.pid')}",
        30,
    )
    if setup.exit_code:
        raise RuntimeError(f"ClawTune sidecar setup failed: {setup.stderr[-2000:]}")
    ready = runtime_executor.execute(
        prefix
        + "for i in $(seq 1 120); do "
        + "curl -fsS http://127.0.0.1:8765/health/ready >/dev/null 2>&1 && exit 0; "
        + "sleep 0.5; done; exit 1",
        70,
    )
    if ready.exit_code:
        raise RuntimeError("ClawTune sidecar did not become ready in the Runtime VM")
    invoke(["plugins", "install", "--link", plugin])
    invoke(["plugins", "enable", "clawbox-cube-tool"])
    invoke(["plugins", "install", "--link", clawtune_plugin])
    invoke(["plugins", "enable", "clawtune"])
    patch = {
        "agents": {"defaults": {"workspace": workspace, "sandbox": {"mode": "off"}}},
        "tools": {
            "allow": ["cube_shell"],
            "deny": ["exec", "process", "read", "write", "edit", "apply_patch",
                     "browser", "canvas", "nodes", "cron", "gateway"],
            "elevated": {"enabled": False},
        },
        "plugins": {"entries": {
            "clawbox-cube-tool": {"enabled": True},
            "clawtune": {"enabled": True, "config": {
                "endpoint": "http://127.0.0.1:8765",
                "mode": "observe", "failOpen": True, "executionBackend": "hook-only",
                "instrumentTools": ["cube_shell"], "enableCgroup": False,
                "enableAffinity": False, "enableNuma": False, "autoStartSidecar": False,
                "securityBoundaryAccepted": True,
                "trace": {"schema_version": 6, "include_raw_events": True,
                          "include_llm_messages": True, "include_tool_outputs": True,
                          "redact_sensitive_data": True, "flush_span_start": True,
                          "trace_dir": trace_dir},
            }},
        }},
    }
    invoke(["config", "patch", "--stdin"], input_value=json.dumps(patch))
    invoke(["onboard", "--non-interactive", "--accept-risk", "--skip-health", "--mode", "local",
            "--auth-choice", "vllm", "--custom-base-url", "http://127.0.0.1:8765/v1",
            "--custom-api-key", f"$ENV:{key_env}", "--custom-model-id", model])
    instruction = (
        "Use cube_shell for every shell, repository, file, build, test, and generated-program "
        "operation. The isolated working directory is /workspace. Never use host-local tools.\n\n"
        f"Task:\n{prompt}"
    )
    result = invoke(["agent", "--local", "--agent", "main", "--session-id", session_id,
                     "--model", f"vllm/{model}", "--message", instruction,
                     "--timeout", str(timeout_seconds), "--json"])
    host_home = output_dir / "openclaw" / session_id
    host_home.mkdir(parents=True, exist_ok=True)
    (host_home / "final-answer.json").write_text(result.stdout, encoding="utf-8")
    runtime_executor.execute(
        prefix
        + f"if [ -s {shlex.quote(home + '/sidecar.pid')} ]; then "
        + f"pid=$(cat {shlex.quote(home + '/sidecar.pid')}); "
        + "case $pid in *[!0-9]*|'') ;; *) kill -TERM \"$pid\" 2>/dev/null || true; "
        + "for i in $(seq 1 20); do kill -0 \"$pid\" 2>/dev/null || break; sleep 0.1; done;; "
        + "esac; fi",
        10,
    )
    trace_listing = runtime_executor.execute(
        prefix + f"find {shlex.quote(trace_dir)} -type f "
        + "\\( -name '*.jsonl' -o -name '*.json' \\) -print",
        30,
    )
    copied_traces: list[str] = []
    host_trace_dir = output_dir / "runtime-traces" / session_id
    host_trace_dir.mkdir(parents=True, exist_ok=True)
    for remote_path in trace_listing.stdout.splitlines():
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
            copied_traces.append(str(target))
    return {"stdout": result.stdout, "stderr": result.stderr, "tool_calls": len(bridge.calls),
            "tool_latencies": [item.duration_s for item in bridge.calls],
            "runtime_traces": copied_traces}
