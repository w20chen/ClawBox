from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from clawbox.experiments.openclaw_driver import CubeToolBridge, run_openclaw
from clawbox.replay.lifecycle import CommandResult


def test_cube_tool_bridge_is_authenticated_and_preserves_result_shape() -> None:
    seen = {}

    def execute(command: str, timeout: float, execution_id: str) -> CommandResult:
        seen.update(command=command, timeout=timeout, execution_id=execution_id)
        return CommandResult(exit_code=7, stdout="out", stderr="err", duration_s=0.25)

    with CubeToolBridge(execute) as bridge:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = json.dumps({"command": "pytest -q", "timeout_seconds": 9,
                           "execution_id": "call-123"}).encode()
        unauthorized = urllib.request.Request(
            bridge.url + "/execute", data=body,
            headers={"content-type": "application/json"}, method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(unauthorized)
        assert error.value.code == 401
        request = urllib.request.Request(
            bridge.url + "/execute", data=body,
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {bridge.token}"}, method="POST",
        )
        result = json.load(opener.open(request))
    assert seen == {"command": "pytest -q", "timeout": 9.0,
                    "execution_id": "call-123"}
    assert result == {"exit_code": 7, "stdout": "out", "stderr": "err",
                      "duration_seconds": 0.25, "execution_id": "call-123"}


def test_openclaw_runner_disables_host_tools_and_uses_secret_env(monkeypatch, tmp_path: Path) -> None:
    commands = []

    class RuntimeExecutor:
        def execute(self, command, _timeout):
            commands.append(command)
            if " agent " in command:
                return CommandResult(0, '{"ok":true}\n', "", 0.1)
            return CommandResult(0, "", "", 0.01)

    monkeypatch.setenv("OPENCLAW_API_KEY", "secret-from-kubernetes")
    with CubeToolBridge(lambda *_: CommandResult(0, "", "", 0.1)) as bridge:
        result = run_openclaw(
            prompt="Create /workspace/result.txt", session_id="session-a",
            configuration={"base_url": "http://model.test/v1", "model": "test-model"},
            bridge=bridge, runtime_executor=RuntimeExecutor(),
            output_dir=tmp_path, timeout_seconds=60,
        )
    patch_command = next(command for command in commands if "config patch" in command)
    encoded = re.search(r"printf %s ([A-Za-z0-9+/=]+) \|", patch_command).group(1)
    config = json.loads(base64.b64decode(encoded))
    assert config["tools"]["allow"] == ["cube_shell"]
    assert {"exec", "read", "write", "apply_patch"} <= set(config["tools"]["deny"])
    assert config["plugins"]["entries"]["clawtune"]["config"]["instrumentTools"] == ["cube_shell"]
    assert config["plugins"]["entries"]["clawtune"]["config"]["endpoint"] == (
        "http://127.0.0.1:8765"
    )
    agent_command = next(command for command in commands if " agent " in command)
    setup_command = next(command for command in commands if "clawtune_sidecar.main" in command)
    onboard_command = next(command for command in commands if " onboard " in command)
    assert "native-kb-pull.py" in setup_command
    assert "http://127.0.0.1:8765/v1" in onboard_command
    assert "Use cube_shell for every" in agent_command
    assert "secret-from-kubernetes" not in "\n".join(commands)
    assert '"${OPENCLAW_API_KEY}"' in "\n".join(commands)
    assert result["tool_calls"] == 0


def test_openclaw_runner_rejects_unsafe_credential_environment(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENCLAW_API_KEY", "secret")
    with CubeToolBridge(lambda *_: CommandResult(0, "", "", 0.1)) as bridge:
        with pytest.raises(ValueError, match="valid environment variable"):
            run_openclaw(
                prompt="test", session_id="session-a",
                configuration={
                    "base_url": "http://model.test/v1", "model": "test-model",
                    "api_key_env": "OPENCLAW_API_KEY;env",
                },
                bridge=bridge, runtime_executor=object(), output_dir=tmp_path,
                timeout_seconds=60,
            )
