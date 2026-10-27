from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from clawbox.experiments.openclaw_driver import (
    NativeSSHConfig,
    NativeSSHRoute,
    RUNTIME_LOCAL_TOOLS,
    TOOL_VM_TOOLS,
    native_ssh_host_key_alias,
    openclaw_real_ssh_path,
    OPENCLAW_SSH_RUNTIME_ROOT,
    OPENCLAW_AUTH_PRELOAD,
    native_tool_workspace_setup_command,
    native_ssh_route,
    native_ssh_target,
    native_tool_bridge_setup_command,
    openclaw_shared_ssh_runtime_directory,
    run_openclaw,
    split_native_ssh_target,
)
from clawbox.replay.lifecycle import CommandResult
from clawbox.experiments.runtime_model_relay import RUNTIME_MODEL_RELAY_SCRIPT


def test_openclaw_shared_runtime_marker_matches_installed_backend() -> None:
    assert openclaw_shared_ssh_runtime_directory("/workspace/") == (
        "/workspace/openclaw-ssh-shared-8198076c"
    )
    assert OPENCLAW_SSH_RUNTIME_ROOT == "/workspace"
    assert native_tool_workspace_setup_command('/testbed') == (
        'set -eu; test -d /testbed; '
        'mkdir -p -- /testbed/openclaw-ssh-shared-8198076c'
    )
    assert native_tool_workspace_setup_command('/workspace') == (
        'set -eu; test -d /workspace; '
        'mkdir -p -- /workspace/openclaw-ssh-shared-8198076c'
    )
    assert '/workspace/repository/openclaw-ssh-shared-' in (
        native_tool_workspace_setup_command('/workspace/repository'))
    with pytest.raises(ValueError, match="absolute POSIX"):
        native_tool_workspace_setup_command('relative')
    assert openclaw_real_ssh_path("session-a") == (
        "/state/openclaw/session-a/bin/ssh.real"
    )


def test_tool_bootstrap_uses_the_same_python_hash_seed_for_record_and_replay():
    command = native_tool_bridge_setup_command()
    assert "export PYTHONHASHSEED=0" in command
    assert "/etc/profile.d/clawbox-experiment.sh" in command
    assert "PYTHONHASHSEED=0 " in command


def test_tool_bootstrap_installs_only_the_configured_apt_proxy() -> None:
    default = native_tool_bridge_setup_command()
    assert "01clawbox-package-cache" not in default

    configured = native_tool_bridge_setup_command(
        apt_cache_proxy_url="http://193.124.7.2:3142",
    )
    assert "/etc/apt/apt.conf.d/01clawbox-package-cache" in configured
    assert 'Acquire::http::Proxy "http://193.124.7.2:3142";' in configured
    assert "http_proxy=" not in configured
    assert "https_proxy=" not in configured


def test_tool_bridge_inherits_only_the_configured_pypi_index() -> None:
    default = native_tool_bridge_setup_command()
    assert "PIP_INDEX_URL=" not in default
    assert "PIP_TRUSTED_HOST=" not in default

    configured = native_tool_bridge_setup_command(
        pypi_cache_index_url="http://193.124.7.2:3141/root/pypi/+simple/",
    )
    assert (
        "PIP_INDEX_URL=http://193.124.7.2:3141/root/pypi/+simple/"
        in configured
    )
    assert "PIP_TRUSTED_HOST=193.124.7.2" in configured
    assert "http_proxy=" not in configured
    assert "https_proxy=" not in configured


class PolicySession:
    url = "http://192.0.2.10:18080"
    token = "policy-token"

    @staticmethod
    def records() -> list[dict]:
        return [{"request": {}, "admission": {}, "completion": {
            "execution_started_at": 10.0, "execution_completed_at": 10.25,
        }}]


@pytest.mark.parametrize("auth_saved", [True, False])
@pytest.mark.parametrize("inherited_options", [None, "--no-warnings"])
def test_auth_preload_uses_exported_store_in_agent_process(
    tmp_path, auth_saved, inherited_options,
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the native agent preload")
    package = tmp_path / "node_modules" / "openclaw"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({
        "name": "openclaw", "type": "module",
        "exports": {"./plugin-sdk/provider-auth": "./provider-auth.mjs"},
    }))
    (package / "provider-auth.mjs").write_text("""
export async function upsertAuthProfileWithLock(value) {
  console.log(JSON.stringify({value, pid: process.pid,
    options: process.env.NODE_OPTIONS ?? null,
    helperEnvPresent: 'CLAWBOX_OPENCLAW_NODE_OPTIONS' in process.env}));
  return process.env.AUTH_SAVED === 'yes' ? {profiles: {}} : null;
}
""")
    agent = package / "openclaw.mjs"
    agent.write_text("console.log(JSON.stringify({agentPid: process.pid}));")
    preload = tmp_path / "auth.mjs"
    preload.write_text(OPENCLAW_AUTH_PRELOAD)
    environment = dict(os.environ)
    environment.update({
        "NODE_OPTIONS": f'--import="{preload.as_uri()}"',
        "CLAWBOX_OPENCLAW_NODE_OPTIONS_SET": "x" if inherited_options is not None else "",
        "CLAWBOX_OPENCLAW_NODE_OPTIONS": inherited_options or "",
        "CLAWTUNE_RUNTIME_ID": "separate-session-b",
        "OPENCLAW_STATE_DIR": str(tmp_path / "session-b-state"),
        "AUTH_SAVED": "yes" if auth_saved else "no",
    })
    result = subprocess.run(
        [node, str(agent), "agent"], env=environment,
        capture_output=True, text=True, timeout=20,
    )
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert records[0]["value"] == {
        "profileId": "vllm:default",
        "credential": {"type": "api_key", "provider": "vllm",
                       "key": "clawtune-runtime.separate-session-b"},
        "agentDir": str(tmp_path / "session-b-state" / "agents" / "main" / "agent"),
    }
    assert records[0]["options"] == inherited_options
    assert records[0]["helperEnvPresent"] is False
    if auth_saved:
        assert result.returncode == 0, result.stderr
        assert records[1]["agentPid"] == records[0]["pid"]
    else:
        assert result.returncode != 0
        assert len(records) == 1
        assert "native session credential initialization failed" in result.stderr


@pytest.mark.parametrize("diagnostic_available", [True, False])
def test_sidecar_readiness_failure_preserves_log_and_original_error(
    monkeypatch, tmp_path, diagnostic_available,
):
    original = TimeoutError("sidecar startup deadline")

    class RuntimeExecutor:
        def execute(self, command, timeout):
            if "/health/ready" in command:
                assert timeout == 310
                raise original
            if "tail -c 65536" in command:
                if not diagnostic_available:
                    raise RuntimeError("guest unreachable")
                return CommandResult(0, "sidecar startup traceback\n", "", 0.01)
            return CommandResult(0, "", "", 0.01)

    monkeypatch.setenv("OPENCLAW_API_KEY", "secret")
    with pytest.raises(TimeoutError) as caught:
        run_openclaw(
            prompt="test", session_id="session-a",
            configuration={"base_url": "http://model.test/v1", "model": "test-model",
                           "runtime_startup_timeout_seconds": 300},
            ssh=NativeSSHConfig(target="executor@192.0.2.20:2222",
                identity_private_key="PRIVATE KEY\n", host_public_key="ssh-ed25519 AAAATEST",
                sandbox_id="tool-a", host_key_alias="clawbox-tool-tool-a"),
            policy_control=PolicySession(), runtime_executor=RuntimeExecutor(),
            output_dir=tmp_path, timeout_seconds=600,
        )
    assert caught.value is original
    directory = tmp_path / "openclaw/session-a"
    if diagnostic_available:
        assert (directory / "sidecar-startup.log").read_text() == "sidecar startup traceback\n"
    else:
        assert (directory / "sidecar-startup-unavailable.txt").read_text() == "guest unreachable"


def test_native_ssh_route_has_an_explicit_epoch_and_stable_tool_alias() -> None:
    route = NativeSSHRoute("tool-a", 2222, 7, "192.0.2.10", 20010)
    assert route.target == "executor@192.0.2.10:20010"
    assert native_ssh_host_key_alias("tool-a") == "clawbox-tool-tool-a"
    with pytest.raises(ValueError, match="epoch"):
        NativeSSHRoute("tool-a", 2222, 0, "192.0.2.10", 20010)


def test_native_ssh_route_requires_cube_mapped_port() -> None:
    with pytest.raises(ValueError, match="mapped port"):
        native_ssh_route(
            type("Endpoint", (), {
                "sandbox_id": "tool-a", "container_port": 2222,
                "address": "192.0.2.10",
            })(),
            epoch=1,
        )


@pytest.mark.parametrize("workspace", ["/workspace", "/testbed"])
@pytest.mark.parametrize("gateway_mode", [None, "api", "replay"])
def test_openclaw_runner_uses_native_ssh_for_all_workspace_tools(
    monkeypatch, tmp_path: Path, gateway_mode, workspace,
) -> None:
    commands = []
    startup_events = []
    startup_begins = []
    startup_waits = []

    class Gateway:
        url = "http://model.test/v1"

        @staticmethod
        def records():
            return []

        @staticmethod
        def replay_completeness():
            return {"complete": True} if gateway_mode == "replay" else None

    class RuntimeExecutor:
        def execute(self, command, _timeout):
            commands.append(command)
            if " agent " in command:
                return CommandResult(
                    0, '{"meta":{"toolSummary":{"failures":0}}}\n', "", 0.1,
                )
            if "cat /state/openclaw/session-a/logs/sidecar.log" in command:
                return CommandResult(0, "sidecar evidence\n", "", 0.01)
            if "tar -cPzhf" in command:
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                    for name, content in (
                        ("/state/clawtune/session-a/traces/native/trace.jsonl",
                         b'{"trace":true}\n'),
                        ("/state/openclaw/session-a/logs/sidecar.log",
                         b"sidecar evidence\n"),
                    ):
                        member = tarfile.TarInfo(name)
                        member.size = len(content)
                        archive.addfile(member, io.BytesIO(content))
                payload = base64.b64encode(stream.getvalue()).decode()
                return CommandResult(0, payload, "", 0.01)
            return CommandResult(0, "", "", 0.01)

    monkeypatch.setenv("OPENCLAW_API_KEY", "secret")
    result = run_openclaw(
        prompt="Create /workspace/result.txt", session_id="session-a",
        configuration={"base_url": "http://model.test/v1", "model": "test-model",
                       "openclaw_exec_yield_ms": 120000},
        ssh=NativeSSHConfig(
            target="executor@2222-tool.cube.local:2222",
            identity_private_key="PRIVATE KEY\n",
            host_public_key="ssh-ed25519 AAAATEST",
            sandbox_id="tool-a", host_key_alias="clawbox-tool-tool-a",
            workspace_root=workspace,
        ),
        policy_control=PolicySession(), runtime_executor=RuntimeExecutor(),
        output_dir=tmp_path, timeout_seconds=60,
        model_gateway=Gateway() if gateway_mode else None,
        startup_begin=lambda: startup_begins.append(len(commands)),
        startup_wait=lambda: startup_waits.append(len(commands)),
        startup_event=startup_events.append,
    )
    patch_command = next(command for command in commands if "set -eu; umask 077" in command)
    encoded = re.search(r"printf %s ([A-Za-z0-9+/=]+) \|", patch_command).group(1)
    config = json.loads(base64.b64decode(encoded))
    sandbox = config["agents"]["defaults"]["sandbox"]
    assert sandbox["backend"] == "ssh"
    assert sandbox["ssh"]["target"] == "executor@2222-tool.cube.local:2222"
    assert sandbox["ssh"]["workspaceRoot"] == workspace
    assert config["tools"]["allow"] == [*TOOL_VM_TOOLS, *RUNTIME_LOCAL_TOOLS]
    assert config["tools"]["sandbox"]["tools"]["allow"] == list(TOOL_VM_TOOLS)
    assert config["tools"]["exec"]["backgroundMs"] == 120000
    assert config["tools"]["exec"]["timeoutSec"] == 120
    assert config["plugins"]["load"]["paths"] == [
        "/opt/clawtune/packages/clawtune-plugin"
    ]
    clawtune = config["plugins"]["entries"]["clawtune"]["config"]
    assert clawtune["failOpen"] is False
    assert clawtune["trace"]["redact_sensitive_data"] is False
    assert clawtune["trace"]["max_messages_bytes"] == 67108864
    assert clawtune["sandboxExecEnvelope"] is True
    assert clawtune["sandboxExecPredictionModel"] == "lattice"
    assert clawtune["instrumentTools"] == list(TOOL_VM_TOOLS)
    assert clawtune["instrumentHosts"] == ["sandbox", "gateway"]
    assert "clawbox-cube-tool" not in json.dumps(config)
    assert "CLAWBOX_POLICY_CONTROL_URL=http://192.0.2.10:18080" in "\n".join(commands)
    assert (tmp_path / "runtime-traces/session-a/native/trace.jsonl").read_bytes() == b'{"trace":true}\n'
    assert sum("tar -cPzhf" in command for command in commands) == 1
    assert "CLAWBOX_POLICY_CONTROL_AUTH=policy-token" in "\n".join(commands)
    assert "CLAWBOX_POLICY_REQUIRE_ENVELOPE=1" in "\n".join(commands)
    assert "CLAWBOX_TOOL_TIMEOUT_SECONDS=300" in "\n".join(commands)
    assert "CLAWBOX_REAL_SSH=/state/openclaw/session-a/bin/ssh.real" in "\n".join(commands)
    assert "OPENCLAW_BASH_YIELD_MS=120000" in "\n".join(commands)
    assert "XDG_CACHE_HOME=/opt/clawtune/cache" in "\n".join(commands)
    assert (
        "CLAWTUNE_KB_SEED=/opt/clawtune/cold-start/tool-resource"
        in "\n".join(commands)
    )
    assert "CLAWTUNE_TOOL_RESOURCE_EBPF_ENABLED=false" in "\n".join(commands)
    assert "timed-clawtune-sidecar.py" in "\n".join(commands)
    assert "sidecar-startup-timing.json" in "\n".join(commands)
    assert "CLAWTUNE_LLM_PROXY_EXPOSE_MODEL=test-model" in "\n".join(commands)
    assert "CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL=test-model" in "\n".join(commands)
    assert "/bin/ssh" in "\n".join(commands)
    assert "install -m 0755 /usr/bin/ssh /state/openclaw/session-a/bin/ssh.real" in "\n".join(commands)
    assert "install -m 0755 /state/openclaw/session-a/bin/ssh /usr/local/bin/ssh" in "\n".join(commands)
    assert "install -m 0755 /state/openclaw/session-a/bin/ssh /usr/bin/ssh" in "\n".join(commands)
    assert "command" not in sandbox["ssh"]
    assert config["agents"]["defaults"]["workspace"] == "/workspace"
    assert config["agents"]["defaults"]["model"] == {"primary": "vllm/test-model"}
    assert "skipBootstrap" not in config["agents"]["defaults"]
    assert config["auth"]["profiles"] == {
        "vllm:default": {"provider": "vllm", "mode": "api_key"},
    }
    assert config["plugins"]["entries"]["vllm"] == {"enabled": True}
    assert config["hooks"]["internal"]["entries"]["session-memory"] == {"enabled": True}
    assert config["tools"]["profile"] == "coding"
    assert "CLAWBOX_POLICY_CONTROL_TOKEN=policy-token" in "\n".join(commands)
    provider = config["models"]["providers"]["vllm"]
    assert provider["timeoutSeconds"] == 120
    assert provider["baseUrl"] == "http://127.0.0.1:8765/v1"
    assert provider["apiKey"] == "VLLM_API_KEY"
    assert provider["api"] == "openai-completions"
    assert provider["models"][0]["id"] == "test-model"
    assert "reasoning" not in provider["models"][0]
    assert not any("config unset" in command or "config set" in command
                   for command in commands)
    assert not any("plugins install" in command or "plugins enable" in command
                   for command in commands)
    assert "clawtune-kb.tar.gz" not in "\n".join(commands)
    assert "clawtune-extra" not in "\n".join(commands)
    assert any(b'"command": profile_command' in base64.b64decode(encoded)
               for command in commands
               for encoded in re.findall(r"printf %s ([A-Za-z0-9+/=]+) \|", command))
    launcher_payloads = [
        base64.b64decode(encoded)
        for command in commands
        for encoded in re.findall(r"printf %s ([A-Za-z0-9+/=]+) \|", command)
        if base64.b64decode(encoded).startswith(b"#!/usr/bin/env python3")
    ]
    assert launcher_payloads
    assert all(b"\r\n" not in payload for payload in launcher_payloads)
    agent_command = next(command for command in commands if " agent " in command)
    assert "--model vllm/test-model" in agent_command
    assert "CLAWTUNE_RUNTIME_ID=session-a" in agent_command
    assert "CLAWTUNE_GATEWAY_ID=session-a" in agent_command
    assert "--import=/state/openclaw/session-a/bin/openclaw-auth.mjs" in agent_command
    assert "OPENCLAW_STATE_DIR=/state/openclaw/session-a/.openclaw/.openclaw" in agent_command
    assert not any(" onboard " in command or "config patch" in command for command in commands)
    assert "--message 'Create /workspace/result.txt'" in agent_command
    assert "Use only sandboxed" not in agent_command
    assert "agent.pid" in agent_command
    assert "exec " in agent_command
    assert result["tool_calls"] == 1
    assert result["tool_latencies"] == [0.25]
    assert result["agent_tool_failures"] == 0
    stage_events = [item for item in startup_events if "stage" in item]
    assert [item["stage"] for item in stage_events] == [
        "runtime_setup", "sidecar_ready", "openclaw_config",
        "agent_runnable",
    ]
    assert all(item["status"] == "ok" for item in stage_events)
    assert len(startup_begins) == 1
    assert len(startup_waits) == 1
    assert startup_begins[0] < startup_waits[0]
    assert startup_waits[0] == commands.index(agent_command)
    assert (tmp_path / "openclaw/session-a/sidecar.log").read_text() == (
        "sidecar evidence\n"
    )


@pytest.mark.parametrize("timeout_seconds", [60, None])
@pytest.mark.parametrize("native_stat", [False, True])
def test_openclaw_runner_detaches_agent_when_runtime_can_pause(
    monkeypatch, tmp_path: Path, timeout_seconds, native_stat,
) -> None:
    commands: list[str] = []
    probes = []
    readiness = iter((None, False, True))

    def file_nonempty(path, timeout):
        probes.append((path, timeout))
        return next(readiness)

    monkeypatch.setattr("clawbox.experiments.openclaw_driver.time.sleep", lambda _: None)

    class RuntimeExecutor:
        def execute(self, command, _timeout):
            commands.append(command)
            return CommandResult(0, "", "", 0.01)

    def resident_poll(command: str, _timeout: float) -> CommandResult:
        commands.append("POLL " + command)
        if "cat /state/openclaw/session-a/completion/agent.exit" in command:
            return CommandResult(0, "0\n", "", 0.01)
        if "cat /state/openclaw/session-a/logs/agent.stdout" in command:
            return CommandResult(0, '{"ok":true}\n', "", 0.01)
        if "cat /state/openclaw/session-a/logs/agent.stderr" in command:
            return CommandResult(0, "", "", 0.01)
        return CommandResult(0, "", "", 0.01)

    monkeypatch.setenv("OPENCLAW_API_KEY", "secret")
    result = run_openclaw(
        prompt="Create /workspace/result.txt", session_id="session-a",
        configuration={"base_url": "http://model.test/v1", "model": "test-model"},
        ssh=NativeSSHConfig(
            target="executor@192.0.2.20:2222",
            identity_private_key="PRIVATE KEY\n",
            host_public_key="ssh-ed25519 AAAATEST",
            sandbox_id="tool-a", host_key_alias="clawbox-tool-tool-a",
        ),
        policy_control=PolicySession(), runtime_executor=RuntimeExecutor(),
        output_dir=tmp_path, timeout_seconds=timeout_seconds,
        resident_poll=resident_poll,
        resident_file_nonempty=file_nonempty if native_stat else None,
    )

    launch = next(command for command in commands if "nohup /bin/sh" in command)
    assert "agent.exit" in launch
    assert "agent.pid" in launch
    assert "--import=/state/openclaw/session-a/bin/openclaw-auth.mjs" in launch
    assert f"--timeout {timeout_seconds if timeout_seconds is not None else 0}" in launch
    assert result["stdout"] == '{"ok":true}\n'
    if native_stat:
        assert probes == [("/state/openclaw/session-a/completion/agent.exit", 10)] * 3
        assert not any(item.startswith("POLL test -s ") for item in commands)


def test_openclaw_runner_cancels_detached_agent_after_arm_failure(
    monkeypatch, tmp_path: Path,
) -> None:
    commands: list[str] = []

    class RuntimeExecutor:
        def execute(self, command, _timeout):
            commands.append(command)
            return CommandResult(0, "", "", 0.01)

    def resident_poll(command: str, _timeout: float) -> CommandResult:
        commands.append("POLL " + command)
        return CommandResult(0, "", "", 0.01)

    monkeypatch.setenv("OPENCLAW_API_KEY", "secret")
    with pytest.raises(InterruptedError, match="cancelled after arm failure"):
        run_openclaw(
            prompt="test", session_id="session-a",
            configuration={"base_url": "http://model.test/v1", "model": "test-model"},
            ssh=NativeSSHConfig(
                target="executor@192.0.2.20:2222",
                identity_private_key="PRIVATE KEY\n",
                host_public_key="ssh-ed25519 AAAATEST",
                sandbox_id="tool-a", host_key_alias="clawbox-tool-tool-a",
            ),
            policy_control=PolicySession(), runtime_executor=RuntimeExecutor(),
            output_dir=tmp_path, timeout_seconds=60,
            resident_poll=resident_poll, cancelled=lambda: True,
            resident_file_nonempty=lambda *_: pytest.fail("cancelled before stat"),
        )

    assert any("POLL kill -TERM" in command for command in commands)


def test_openclaw_resident_wait_replaces_repeated_status_polling(
    monkeypatch, tmp_path: Path,
) -> None:
    commands: list[str] = []
    waits: list[tuple[str, float]] = []

    class RuntimeExecutor:
        def execute(self, command, _timeout):
            commands.append(command)
            return CommandResult(0, "", "", 0.01)

    def resident_poll(command: str, _timeout: float) -> CommandResult:
        commands.append("POLL " + command)
        if "cat /state/openclaw/session-a/completion/agent.exit" in command:
            return CommandResult(0, "0\n", "", 0.01)
        if "cat /state/openclaw/session-a/logs/agent.stdout" in command:
            return CommandResult(0, '{"ok":true}\n', "", 0.01)
        return CommandResult(0, "", "", 0.01)

    def resident_wait(path: str, timeout: float) -> CommandResult:
        waits.append((path, timeout))
        return CommandResult(0, "", "", 0.01)

    monkeypatch.setenv("OPENCLAW_API_KEY", "secret")
    run_openclaw(
        prompt="test", session_id="session-a",
        configuration={"base_url": "http://model.test/v1", "model": "test-model"},
        ssh=NativeSSHConfig(
            target="executor@192.0.2.20:2222",
            identity_private_key="PRIVATE KEY\n",
            host_public_key="ssh-ed25519 AAAATEST",
            sandbox_id="tool-a", host_key_alias="clawbox-tool-tool-a",
        ),
        policy_control=PolicySession(), runtime_executor=RuntimeExecutor(),
        output_dir=tmp_path, timeout_seconds=60,
        resident_poll=resident_poll, resident_wait=resident_wait,
    )

    assert waits == [("/state/openclaw/session-a/completion/agent.exit", 60.0)]
    assert not any(item.startswith("POLL test -s ") for item in commands)


def test_openclaw_failure_preserves_complete_agent_streams(
    monkeypatch, tmp_path: Path,
) -> None:
    class RuntimeExecutor:
        def execute(self, _command, _timeout):
            return CommandResult(0, "", "", 0.01)

    stderr = "fatal-first-line\n" + ("diagnostic\n" * 500) + "fatal-last-line\n"

    def resident_poll(command: str, _timeout: float) -> CommandResult:
        if "cat /state/openclaw/session-a/completion/agent.exit" in command:
            return CommandResult(0, "7\n", "", 0.01)
        if "cat /state/openclaw/session-a/logs/agent.stdout" in command:
            return CommandResult(0, "partial stdout\n", "", 0.01)
        if "cat /state/openclaw/session-a/logs/agent.stderr" in command:
            return CommandResult(0, stderr, "", 0.01)
        return CommandResult(0, "", "", 0.01)

    monkeypatch.setenv("OPENCLAW_API_KEY", "secret")
    with pytest.raises(RuntimeError, match=r"exit 7.*full log") as error:
        run_openclaw(
            prompt="test", session_id="session-a",
            configuration={"base_url": "http://model.test/v1", "model": "test-model"},
            ssh=NativeSSHConfig(
                target="executor@192.0.2.20:2222",
                identity_private_key="PRIVATE KEY\n",
                host_public_key="ssh-ed25519 AAAATEST",
                sandbox_id="tool-a", host_key_alias="clawbox-tool-tool-a",
            ),
            policy_control=PolicySession(), runtime_executor=RuntimeExecutor(),
            output_dir=tmp_path, timeout_seconds=60,
            resident_poll=resident_poll,
        )
    assert "fatal-first-line" in str(error.value)
    assert "fatal-last-line" in str(error.value)
    assert (tmp_path / "openclaw/session-a/agent.stdout.log").read_text() == "partial stdout\n"
    assert (tmp_path / "openclaw/session-a/agent.stderr.log").read_text() == stderr


def test_checkpoint_relay_keeps_clawtune_in_model_path(tmp_path: Path) -> None:
    commands: list[str] = []

    class RuntimeExecutor:
        def execute(self, command, _timeout):
            commands.append(command)
            if " agent " in command:
                return CommandResult(0, '{"ok":true}\n', "", 0.1)
            return CommandResult(0, "", "", 0.01)

    class Gateway:
        url = "http://192.0.2.30:18081/v1"

        @staticmethod
        def records():
            return []

        @staticmethod
        def replay_completeness():
            return {"complete": True}

    compile(RUNTIME_MODEL_RELAY_SCRIPT, "model-relay.py", "exec")
    result = run_openclaw(
        prompt="test", session_id="session-relay",
        configuration={"model": "test-model"},
        ssh=NativeSSHConfig(
            target="executor@192.0.2.20:2222",
            identity_private_key="PRIVATE KEY\n",
            host_public_key="ssh-ed25519 AAAATEST", sandbox_id="tool-a",
            host_key_alias="clawbox-tool-tool-a",
        ),
        policy_control=PolicySession(), runtime_executor=RuntimeExecutor(),
        output_dir=tmp_path, timeout_seconds=60,
        model_gateway=Gateway(), checkpoint_relay=True,
    )
    assert any("CLAWBOX_RELAY_TIMEOUT_SECONDS=120" in command for command in commands)
    setup = "\n".join(commands)
    assert "model-relay.py" in setup
    assert "CLAWBOX_RELAY_UPSTREAM=http://192.0.2.30:18081/v1" in setup
    assert "CLAWTUNE_LLM_UPSTREAM_BASE_URL=http://127.0.0.1:8766/v1" in setup
    assert "http://127.0.0.1:8766/healthz" in "\n".join(commands)
    assert result["checkpoint_model_relay"] is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("executor@tool.example:2200", ("executor", "tool.example", 2200)),
        ("executor@[2001:db8::10]:2200", ("executor", "2001:db8::10", 2200)),
        ("tool.example", ("executor", "tool.example", 22)),
    ],
)
def test_native_ssh_target_preserves_cube_port(value, expected) -> None:
    assert split_native_ssh_target(value) == expected


def test_native_ssh_target_does_not_append_a_second_port() -> None:
    target = native_ssh_target("tool.example:2200", port=2222)
    assert target == "executor@tool.example:2200"
    assert split_native_ssh_target(target) == ("executor", "tool.example", 2200)


def test_native_tool_bridge_setup_is_explicit_and_waits_for_port() -> None:
    command = native_tool_bridge_setup_command()
    assert "CLAWBOX_TOOL_HOST_KEY_B64" in command
    assert "nohup env TOOL_BRIDGE_HOST_KEY" in command
    assert ":08AE" in command
    assert "exit 1" in command
    assert "CLAWTUNE_GUEST_COLLECTOR_HELPER" in command
    assert "test -S /run/clawtune/guest-collector.sock" in command
    assert "grep -q 'guest collector ready'" in command
    assert 'zcat /proc/config.gz | cmp -s' in command
    assert "pkill" not in command
    assert "TOOL_EXEC_TIMEOUT_SECONDS=300" in command

    restart = native_tool_bridge_setup_command(
        restart=True, command_timeout_seconds=600,
    )
    assert "pkill -x tool-bridge" in restart
    assert "guest-collector.sock" in restart
    assert "TOOL_EXEC_TIMEOUT_SECONDS=600" in restart


def test_native_tool_bridge_setup_only_removes_dev_fd_for_replay() -> None:
    assert "rm -f /dev/fd" not in native_tool_bridge_setup_command()
    assert "rm -f /dev/fd" not in native_tool_bridge_setup_command(restart=True)


def test_native_ssh_target_rejects_malformed_explicit_port() -> None:
    with pytest.raises(ValueError, match="target port"):
        split_native_ssh_target("executor@[2001:db8::10]:")


def test_openclaw_runner_rejects_unsafe_credential_environment(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENCLAW_API_KEY", "secret")
    with pytest.raises(ValueError, match="valid environment variable"):
        run_openclaw(
            prompt="test", session_id="session-a",
            configuration={"base_url": "http://model.test/v1", "model": "test-model",
                           "api_key_env": "OPENCLAW_API_KEY;env"},
            ssh=NativeSSHConfig("executor@tool:2222", "private", "ssh-ed25519 public"),
            policy_control=PolicySession(), runtime_executor=object(),
            output_dir=tmp_path, timeout_seconds=60,
        )


@pytest.mark.parametrize("value", [True, 9, 120001, 10.5, "ten"])
def test_openclaw_runner_rejects_invalid_exec_yield(value, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENCLAW_API_KEY", "secret")
    with pytest.raises(ValueError, match="openclaw_exec_yield_ms"):
        run_openclaw(
            prompt="test", session_id="session-a",
            configuration={"base_url": "http://model.test/v1", "model": "test-model",
                           "openclaw_exec_yield_ms": value},
            ssh=NativeSSHConfig(
                "executor@tool:2222", "private", "ssh-ed25519 public",
                sandbox_id="tool-a",
            ),
            policy_control=PolicySession(), runtime_executor=object(),
            output_dir=tmp_path, timeout_seconds=60,
        )
