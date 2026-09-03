from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from clawbox.experiments.openclaw_driver import CubeToolBridge, WorkerBridge, run_openclaw
from clawbox.replay.lifecycle import CommandResult


def test_cube_tool_bridge_is_authenticated_and_preserves_result_shape() -> None:
    seen = {}

    def execute(command: str, timeout: float, execution_id: str) -> CommandResult:
        seen.update(command=command, timeout=timeout, execution_id=execution_id)
        return CommandResult(exit_code=7, stdout="out", stderr="err", duration_s=0.25)

    with CubeToolBridge(execute) as bridge:
        assert bridge.bind_host == "127.0.0.1"
        assert bridge.advertised_host == "127.0.0.1"
        assert bridge.actual_port > 0
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
    assert bridge.requests[0]["path"] == "/execute"
    assert bridge.requests[0]["source_ip"] in {"127.0.0.1", "::1"}


def test_cube_tool_bridge_downward_api_host_binds_all_interfaces(monkeypatch) -> None:
    monkeypatch.setenv("CLAWBOX_BRIDGE_HOST", "192.0.2.10")
    with CubeToolBridge(lambda *_: CommandResult(0, "", "", 0.0)) as bridge:
        assert bridge.bind_host == "0.0.0.0"
        assert bridge.advertised_host == "192.0.2.10"
        assert bridge.startup == {
            "bind_host": "0.0.0.0", "advertised_host": "192.0.2.10",
            "actual_port": bridge.actual_port, "url": bridge.url,
        }


def test_worker_bridge_routes_tokens_to_their_session() -> None:
    calls = []

    def execute(session, command, timeout, execution_id):
        calls.append((session, execution_id))
        return CommandResult(0, session, "", 0.01)

    # Use a local-only bind in the unit test; production construction uses the
    # fixed 0.0.0.0:18080 defaults.
    bridge = WorkerBridge(advertise_host="127.0.0.1", advertised_port=18080,
                          bind_host="127.0.0.1", bind_port=18080)
    with bridge:
        first = bridge.register("session-a", lambda c, t, e: execute("a", c, t, e))
        second = bridge.register("session-b", lambda c, t, e: execute("b", c, t, e))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(token, session_id, execution_id):
            body = json.dumps({"command": "true", "execution_id": execution_id,
                               "session_id": session_id}).encode()
            return urllib.request.Request(first.url + "/execute", data=body, method="POST",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

        assert json.load(opener.open(request(first.token, "session-a", "exec-a")))["stdout"] == "a"
        wrong = request(second.token, "session-a", "exec-a")
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(wrong)
        assert error.value.code == 403
        unknown = request("not-a-token", "session-a", "exec-a")
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(unknown)
        assert error.value.code == 401
        assert json.load(opener.open(request(second.token, "session-b", "exec-b")))["stdout"] == "b"
        assert first.close()
        assert second.close()
    assert calls == [("a", "exec-a"), ("b", "exec-b")]
    assert first.token not in repr(bridge.requests)
    assert second.token not in repr(bridge.requests)


def test_worker_bridge_scales_sessions_and_has_no_head_of_line_blocking() -> None:
    bridge = WorkerBridge(advertise_host="127.0.0.1", advertised_port=18080,
                          bind_host="127.0.0.1", bind_port=18080)
    executed: dict[str, str] = {}
    started: dict[str, float] = {}
    finished: dict[str, float] = {}

    def make_executor(session_id: str):
        def execute(command: str, _timeout: float, execution_id: str) -> CommandResult:
            started[execution_id] = time.monotonic()
            if command == "long":
                time.sleep(0.35)
            else:
                time.sleep(0.01)
            executed[execution_id] = session_id
            finished[execution_id] = time.monotonic()
            return CommandResult(0, session_id, "", 0.01)
        return execute

    def post(opener, session, command, execution_id):
        body = json.dumps({"command": command, "execution_id": execution_id,
                           "session_id": session.session_id}).encode()
        request = urllib.request.Request(
            session.url + "/execute", data=body, method="POST",
            headers={"Authorization": f"Bearer {session.token}",
                     "Content-Type": "application/json"},
        )
        return json.load(opener.open(request))

    with bridge:
        sessions = {}
        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(bridge.register, f"session-{i:03d}",
                                   make_executor(f"session-{i:03d}"),
                                   task_id="task-stress", tool_sandbox_id=f"tool-{i:03d}")
                       for i in range(100)]
            for i, future in enumerate(futures):
                sessions[i] = future.result()
        assert bridge.session_count == 100
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for level in (1, 8, 20, 40, 60):
            with ThreadPoolExecutor(max_workers=level) as pool:
                futures = [pool.submit(post, opener, sessions[i], "short",
                                       f"stress-{level}-{i}") for i in range(level)]
                responses = [future.result() for future in futures]
            assert {response["session_id"] for response in responses} == {
                f"session-{i:03d}" for i in range(level)
            }
        with ThreadPoolExecutor(max_workers=12) as pool:
            long_future = pool.submit(post, opener, sessions[0], "long", "hol-long")
            time.sleep(0.03)
            short_futures = [pool.submit(post, opener, sessions[i], "short", f"hol-{i}")
                             for i in range(1, 9)]
            short_responses = [future.result() for future in short_futures]
            long_response = long_future.result()
        assert long_response["session_id"] == "session-000"
        assert all(response["session_id"] != "session-000" for response in short_responses)
        assert max(finished[f"hol-{i}"] for i in range(1, 9)) < finished["hol-long"]
        assert all(executed[key] == f"session-{int(key.rsplit('-', 1)[1]):03d}"
                   for key in executed if key.startswith("stress-"))
        for session in sessions.values():
            assert session.close(timeout=5)
        assert bridge.session_count == 0


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
    assert "CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=false" in setup_command
    assert "CLAWTUNE_REPO_KEY=" in setup_command
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
