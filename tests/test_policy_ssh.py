from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "clawbox_policy_ssh", Path(__file__).parents[1] / "scripts" / "clawbox-policy-ssh.py",
)
assert _SPEC is not None and _SPEC.loader is not None
policy_ssh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(policy_ssh)


def _argv(tool_name: str = "exec") -> list[str]:
    metadata = json.dumps(
        {"v": 1, "execution_id": "exec-a", "tool_name": tool_name},
        separators=(",", ":"),
    )
    return [
        "-F", "/state/openclaw/ssh/config", "openclaw-sandbox",
        policy_ssh.PREFIX + metadata + "\nprintf /run/identity",
    ]


def test_policy_route_is_injected_per_invocation_without_replacing_ssh_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWBOX_REAL_SSH", "/fake/ssh")
    monkeypatch.setenv("CLAWBOX_POLICY_CONTROL_URL", "http://policy.test")
    monkeypatch.setenv("CLAWBOX_POLICY_CONTROL_TOKEN", "token")
    monkeypatch.setenv("CLAWBOX_POLICY_SESSION_ID", "session-a")
    monkeypatch.setenv("CLAWBOX_TOOL_SANDBOX_ID", "tool-a")
    monkeypatch.setenv("CLAWBOX_SSH_HOST_KEY_ALIAS", "clawbox-tool-tool-a")
    posted: list[tuple[str, dict]] = []

    def post(path: str, body: dict, *, attempts: int) -> dict:
        posted.append((path, body))
        if path.endswith("/admit"):
            return {
                "decision": "ADMIT", "sandbox_id": "tool-a", "epoch": 9,
                "container_port": 2222, "host": "192.0.2.20", "port": 20020,
            }
        return {"status": "COMPLETED"}

    class Child:
        def wait(self) -> int:
            return 0

    launched: list[list[str]] = []
    monkeypatch.setattr(policy_ssh, "_post", post)
    monkeypatch.setattr(policy_ssh.subprocess, "Popen", lambda argv: (launched.append(argv) or Child()))
    monkeypatch.setattr(sys, "argv", ["clawbox-policy-ssh.py", *_argv()])

    assert policy_ssh.main() == 0
    assert len(launched) == 1
    command = launched[0]
    assert command[0] == "/fake/ssh"
    assert "-F" in command and "/state/openclaw/ssh/config" in command
    assert "-o" in command
    assert "HostName=192.0.2.20" in command
    assert "Port=20020" in command
    assert "HostKeyAlias=clawbox-tool-tool-a" in command
    assert command[-2] == "openclaw-sandbox"
    assert command[-1].endswith("\nprintf /run/identity")
    assert [path for path, _body in posted] == ["/v1/tool/admit", "/v1/tool/complete"]
    completion = posted[-1][1]
    assert completion["endpoint_sandbox_id"] == "tool-a"
    assert completion["endpoint_epoch"] == 9
    assert completion["ssh_reaped_at"] <= completion["execution_completed_at"]


@pytest.mark.parametrize(
    "tool_name", ("exec", "process", "read", "write", "edit", "apply_patch"),
)
def test_each_tool_vm_operation_keeps_its_native_policy_operation(
    monkeypatch: pytest.MonkeyPatch, tool_name: str,
) -> None:
    """Every OpenClaw Tool-VM operation must carry the execution envelope."""
    monkeypatch.setenv("CLAWBOX_REAL_SSH", "/fake/ssh")
    monkeypatch.setenv("CLAWBOX_POLICY_CONTROL_URL", "http://policy.test")
    monkeypatch.setenv("CLAWBOX_POLICY_CONTROL_TOKEN", "token")
    monkeypatch.setenv("CLAWBOX_POLICY_SESSION_ID", "session-a")
    monkeypatch.setenv("CLAWBOX_TOOL_SANDBOX_ID", "tool-a")
    monkeypatch.setenv("CLAWBOX_SSH_HOST_KEY_ALIAS", "clawbox-tool-tool-a")
    posted: list[tuple[str, dict]] = []

    def post(path: str, body: dict, *, attempts: int) -> dict:
        posted.append((path, body))
        if path.endswith("/admit"):
            return {
                "decision": "ADMIT", "sandbox_id": "tool-a", "epoch": 1,
                "container_port": 2222, "host": "192.0.2.20", "port": 20020,
            }
        return {"status": "COMPLETED"}

    class Child:
        def wait(self) -> int:
            return 0

    monkeypatch.setattr(policy_ssh, "_post", post)
    monkeypatch.setattr(policy_ssh.subprocess, "Popen", lambda _argv: Child())
    monkeypatch.setattr(sys, "argv", ["clawbox-policy-ssh.py", *_argv(tool_name)])

    assert policy_ssh.main() == 0
    assert posted[0][1]["operation"] == tool_name
    assert posted[1][1]["operation"] == tool_name


def test_unenveloped_agent_ssh_is_rejected_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWBOX_POLICY_REQUIRE_ENVELOPE", "1")
    monkeypatch.setenv("CLAWBOX_REAL_SSH", "/fake/ssh")
    monkeypatch.setattr(sys, "argv", ["clawbox-policy-ssh.py", "tool-a", "true"])

    def fail_spawn(_argv: list[str]):
        raise AssertionError("unenveloped Agent operation reached OpenSSH")

    monkeypatch.setattr(policy_ssh.subprocess, "call", fail_spawn)
    assert policy_ssh.main() == 125


def test_policy_rejects_cross_tool_endpoint_before_spawning_ssh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWBOX_REAL_SSH", "/fake/ssh")
    monkeypatch.setenv("CLAWBOX_POLICY_CONTROL_URL", "http://policy.test")
    monkeypatch.setenv("CLAWBOX_POLICY_CONTROL_TOKEN", "token")
    monkeypatch.setenv("CLAWBOX_POLICY_SESSION_ID", "session-a")
    monkeypatch.setenv("CLAWBOX_TOOL_SANDBOX_ID", "tool-a")
    monkeypatch.setenv("CLAWBOX_SSH_HOST_KEY_ALIAS", "clawbox-tool-tool-a")
    posted: list[str] = []

    def post(path: str, _body: dict, *, attempts: int) -> dict:
        posted.append(path)
        return {
            "decision": "ADMIT", "sandbox_id": "tool-b", "epoch": 1,
            "container_port": 2222, "host": "192.0.2.21", "port": 20021,
        }

    def fail_spawn(_argv: list[str]):
        raise AssertionError("cross-Tool route reached subprocess")

    monkeypatch.setattr(policy_ssh, "_post", post)
    monkeypatch.setattr(policy_ssh.subprocess, "Popen", fail_spawn)
    monkeypatch.setattr(sys, "argv", ["clawbox-policy-ssh.py", *_argv()])

    assert policy_ssh.main() == 125
    assert posted == ["/v1/tool/admit"]


def test_policy_rejects_non_ssh_container_port_before_spawning_ssh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWBOX_REAL_SSH", "/fake/ssh")
    monkeypatch.setenv("CLAWBOX_POLICY_CONTROL_URL", "http://policy.test")
    monkeypatch.setenv("CLAWBOX_POLICY_CONTROL_TOKEN", "token")
    monkeypatch.setenv("CLAWBOX_POLICY_SESSION_ID", "session-a")
    monkeypatch.setenv("CLAWBOX_TOOL_SANDBOX_ID", "tool-a")
    monkeypatch.setenv("CLAWBOX_SSH_HOST_KEY_ALIAS", "clawbox-tool-tool-a")
    posted: list[str] = []

    def post(path: str, _body: dict, *, attempts: int) -> dict:
        posted.append(path)
        return {
            "decision": "ADMIT", "sandbox_id": "tool-a", "epoch": 1,
            "container_port": 49983, "host": "192.0.2.20", "port": 20020,
        }

    def fail_spawn(_argv: list[str]):
        raise AssertionError("non-SSH container port reached subprocess")

    monkeypatch.setattr(policy_ssh, "_post", post)
    monkeypatch.setattr(policy_ssh.subprocess, "Popen", fail_spawn)
    monkeypatch.setattr(sys, "argv", ["clawbox-policy-ssh.py", *_argv()])

    assert policy_ssh.main() == 125
    assert posted == ["/v1/tool/admit"]


def test_policy_does_not_complete_while_ssh_child_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAWBOX_REAL_SSH", "/fake/ssh")
    monkeypatch.setenv("CLAWBOX_POLICY_CONTROL_URL", "http://policy.test")
    monkeypatch.setenv("CLAWBOX_POLICY_CONTROL_TOKEN", "token")
    monkeypatch.setenv("CLAWBOX_POLICY_SESSION_ID", "session-a")
    monkeypatch.setenv("CLAWBOX_TOOL_SANDBOX_ID", "tool-a")
    monkeypatch.setenv("CLAWBOX_SSH_HOST_KEY_ALIAS", "clawbox-tool-tool-a")
    posted: list[tuple[str, dict]] = []
    child_started = threading.Event()
    release_child = threading.Event()

    def post(path: str, body: dict, *, attempts: int) -> dict:
        posted.append((path, body))
        if path.endswith("/admit"):
            return {
                "decision": "ADMIT", "sandbox_id": "tool-a", "epoch": 10,
                "container_port": 2222, "host": "192.0.2.20", "port": 20020,
            }
        return {"status": "COMPLETED"}

    class Child:
        def wait(self) -> int:
            child_started.set()
            if not release_child.wait(timeout=2):
                raise AssertionError("test child was not released")
            return 0

    monkeypatch.setattr(policy_ssh, "_post", post)
    monkeypatch.setattr(policy_ssh.subprocess, "Popen", lambda _argv: Child())
    monkeypatch.setattr(sys, "argv", ["clawbox-policy-ssh.py", *_argv()])

    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(policy_ssh.main()))
    thread.start()
    assert child_started.wait(timeout=2)
    time.sleep(0.02)
    assert [path for path, _body in posted] == ["/v1/tool/admit"]
    release_child.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result == [0]
    assert [path for path, _body in posted] == ["/v1/tool/admit", "/v1/tool/complete"]
    completion = posted[-1][1]
    assert completion["ssh_reaped_at"] <= completion["execution_completed_at"]
