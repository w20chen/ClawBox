from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from clawbox.experiments.openclaw_driver import (
    NativeSSHConfig,
    native_ssh_target,
    native_tool_bridge_setup_command,
    native_ssh_target_from_env,
    run_openclaw,
    split_native_ssh_target,
)
from clawbox.replay.lifecycle import CommandResult


class PolicySession:
    url = "http://192.0.2.10:18080"
    token = "policy-token"

    @staticmethod
    def records() -> list[dict]:
        return [{"request": {}, "admission": {}, "completion": {
            "execution_started_at": 10.0, "execution_completed_at": 10.25,
        }}]


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
    assert config["tools"]["allow"] == [
        "exec", "process", "read", "write", "edit", "apply_patch",
    ]
    clawtune = config["plugins"]["entries"]["clawtune"]["config"]
    assert clawtune["failOpen"] is False
    assert clawtune["sandboxExecEnvelope"] is True
    assert clawtune["instrumentTools"] == config["tools"]["allow"]
    assert "clawbox-cube-tool" not in json.dumps(config)
    assert "CLAWBOX_POLICY_CONTROL_URL=http://192.0.2.10:18080" in "\n".join(commands)
    assert "CLAWBOX_POLICY_REQUIRE_ENVELOPE=1" in "\n".join(commands)
    assert result["tool_calls"] == 1
    assert result["tool_latencies"] == [0.25]


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


def test_native_ssh_target_from_env_requires_explicit_raw_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("CLAWBOX_NATIVE_SSH_TARGET", raising=False)
    monkeypatch.delenv("CLAWBOX_NATIVE_SSH_HOST", raising=False)
    with pytest.raises(ValueError, match="get_host is HTTP-only"):
        native_ssh_target_from_env()


def test_native_ssh_target_from_env_renders_per_sandbox_target(monkeypatch) -> None:
    monkeypatch.setenv(
        "CLAWBOX_NATIVE_SSH_TARGET",
        "executor@192.168.3.166:20055",
    )
    assert native_ssh_target_from_env(sandbox_id="sandbox-a") == (
        "executor@192.168.3.166:20055"
    )


def test_native_ssh_target_from_env_supports_host_and_port(monkeypatch) -> None:
    monkeypatch.delenv("CLAWBOX_NATIVE_SSH_TARGET", raising=False)
    monkeypatch.setenv("CLAWBOX_NATIVE_SSH_HOST", "192.168.3.166")
    monkeypatch.setenv("CLAWBOX_NATIVE_SSH_PORT", "20055")
    assert native_ssh_target_from_env() == "executor@192.168.3.166:20055"


def test_native_tool_bridge_setup_is_explicit_and_waits_for_port() -> None:
    command = native_tool_bridge_setup_command()
    assert "CLAWBOX_TOOL_HOST_KEY_B64" in command
    assert "nohup env TOOL_BRIDGE_HOST_KEY" in command
    assert ":08AE" in command
    assert "exit 1" in command


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
