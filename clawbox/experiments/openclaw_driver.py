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


class CubeToolBridge:
    """Expose one authenticated session executor to its Runtime VM."""

    def __init__(self, execute: Callable[[str, float, str], CommandResult], *,
                 advertise_host: str | None = None) -> None:
        self._execute = execute
        self.token = secrets.token_urlsafe(32)
        self.calls: list[CommandResult] = []
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
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

        self.advertise_host = advertise_host or os.environ.get("CLAWBOX_BRIDGE_HOST") or "127.0.0.1"
        bind_host = "0.0.0.0" if advertise_host or os.environ.get("CLAWBOX_BRIDGE_HOST") else "127.0.0.1"
        self.server = ThreadingHTTPServer((bind_host, 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="cube-tool-bridge", daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.advertise_host}:{self.server.server_port}"

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

    runtime_executor.execute(prefix + f"mkdir -p {shlex.quote(workspace)} {shlex.quote(trace_dir)}", 30)
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
            "--auth-choice", "vllm", "--custom-base-url", base_url,
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
    trace_listing = runtime_executor.execute(
        prefix + f"find {shlex.quote(trace_dir)} -maxdepth 1 -type f -name '*.jsonl' -print", 30,
    )
    copied_traces: list[str] = []
    host_trace_dir = output_dir / "runtime-traces" / session_id
    host_trace_dir.mkdir(parents=True, exist_ok=True)
    for remote_path in trace_listing.stdout.splitlines():
        if not remote_path.startswith(trace_dir + "/"):
            continue
        encoded = runtime_executor.execute(prefix + f"base64 -w0 {shlex.quote(remote_path)}", 30)
        if encoded.exit_code == 0:
            target = host_trace_dir / Path(remote_path).name
            target.write_bytes(base64.b64decode(encoded.stdout))
            copied_traces.append(str(target))
    return {"stdout": result.stdout, "stderr": result.stderr, "tool_calls": len(bridge.calls),
            "tool_latencies": [item.duration_s for item in bridge.calls],
            "runtime_traces": copied_traces}
