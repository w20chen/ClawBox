from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from clawbox.experiments.openclaw_driver import (
    NativeSSHConfig,
    NativeSSHRoute,
    RUNTIME_LOCAL_TOOLS,
    TOOL_VM_TOOLS,
    native_ssh_host_key_alias,
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


class PolicySession:
    url = "http://192.0.2.10:18080"
    token = "policy-token"

    @staticmethod
    def records() -> list[dict]:
        return [{"request": {}, "admission": {}, "completion": {
            "execution_started_at": 10.0, "execution_completed_at": 10.25,
        }}]


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


def test_openclaw_runner_uses_native_ssh_for_all_workspace_tools(
    monkeypatch, tmp_path: Path,
) -> None:
    commands = []

    class RuntimeExecutor:
        def execute(self, command, _timeout):
            commands.append(command)
            if " agent " in command:
                return CommandResult(0, '{"ok":true}\n', "", 0.1)
            return CommandResult(0, "", "", 0.01)

    monkeypatch.setenv("OPENCLAW_API_KEY", "secret")
    result = run_openclaw(
        prompt="Create /workspace/result.txt", session_id="session-a",
        configuration={"base_url": "http://model.test/v1", "model": "test-model"},
        ssh=NativeSSHConfig(
            target="executor@2222-tool.cube.local:2222",
            identity_private_key="PRIVATE KEY\n",
            host_public_key="ssh-ed25519 AAAATEST",
            sandbox_id="tool-a", host_key_alias="clawbox-tool-tool-a",
        ),
        policy_control=PolicySession(), runtime_executor=RuntimeExecutor(),
        output_dir=tmp_path, timeout_seconds=60,
    )
    patch_command = next(command for command in commands if "config patch" in command)
    encoded = re.search(r"printf %s ([A-Za-z0-9+/=]+) \|", patch_command).group(1)
    config = json.loads(base64.b64decode(encoded))
    sandbox = config["agents"]["defaults"]["sandbox"]
    assert sandbox["backend"] == "ssh"
    assert sandbox["ssh"]["target"] == "executor@2222-tool.cube.local:2222"
    assert config["tools"]["allow"] == [*TOOL_VM_TOOLS, *RUNTIME_LOCAL_TOOLS]
    assert config["tools"]["sandbox"]["tools"]["allow"] == list(TOOL_VM_TOOLS)
    clawtune = config["plugins"]["entries"]["clawtune"]["config"]
    assert clawtune["failOpen"] is False
    assert clawtune["sandboxExecEnvelope"] is True
    assert clawtune["instrumentTools"] == list(TOOL_VM_TOOLS)
    assert clawtune["instrumentHosts"] == ["sandbox", "gateway"]
    assert "clawbox-cube-tool" not in json.dumps(config)
    assert "CLAWBOX_POLICY_CONTROL_URL=http://192.0.2.10:18080" in "\n".join(commands)
    assert "CLAWBOX_POLICY_CONTROL_AUTH=policy-token" in "\n".join(commands)
    assert "CLAWBOX_POLICY_REQUIRE_ENVELOPE=1" in "\n".join(commands)
    assert "/bin/ssh" in "\n".join(commands)
    assert config["agents"]["defaults"]["sandbox"]["ssh"]["command"].endswith(
        "/bin/ssh"
    )
    assert "exec /usr/local/bin/ssh" in base64.b64decode(
        re.findall(r"printf %s ([A-Za-z0-9+/=]+) \|", commands[0])[2]
    ).decode()
    agent_command = next(command for command in commands if " agent " in command)
    assert "agent.pid" in agent_command
    assert "exec " in agent_command
    assert result["tool_calls"] == 1
    assert result["tool_latencies"] == [0.25]


def test_openclaw_runner_detaches_agent_when_runtime_can_pause(
    monkeypatch, tmp_path: Path,
) -> None:
    commands: list[str] = []

    class RuntimeExecutor:
        def execute(self, command, _timeout):
            commands.append(command)
            return CommandResult(0, "", "", 0.01)

    def resident_poll(command: str, _timeout: float) -> CommandResult:
        commands.append("POLL " + command)
        if "cat /state/openclaw/session-a/agent.exit" in command:
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
        output_dir=tmp_path, timeout_seconds=60,
        resident_poll=resident_poll,
    )

    launch = next(command for command in commands if "nohup /bin/sh" in command)
    assert "agent.exit" in launch
    assert "agent.pid" in launch
    assert result["stdout"] == '{"ok":true}\n'


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
    setup = commands[0]
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
    assert "pkill" not in command

    restart = native_tool_bridge_setup_command(restart=True)
    assert "pkill -x tool-bridge" in restart
    assert "guest-collector.sock" in restart


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
