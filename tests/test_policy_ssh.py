from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "clawbox-policy-ssh.py"
SPEC = importlib.util.spec_from_file_location("clawbox_policy_ssh", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
policy_ssh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy_ssh)


def test_admission_route_rewrites_the_current_ssh_invocation() -> None:
    envelope = '__CBX_EXEC_1__{"v":1,"execution_id":"exec-1"}\nprintf ok'
    original = [
        "-i", "/state/id", "-o", "BatchMode=yes", "-p", "20001",
        "executor@192.0.2.10", envelope,
    ]
    routed = policy_ssh._route_argv(original, "executor@192.0.2.10:20099")
    assert routed[-2] == "executor@192.0.2.10"
    assert routed[routed.index("-p") + 1] == "20099"
    assert original[original.index("-p") + 1] == "20001"


def test_admission_route_adds_port_and_supports_ipv6() -> None:
    envelope = '__CBX_EXEC_1__{"v":1,"execution_id":"exec-1"}\ntrue'
    routed = policy_ssh._route_argv(
        ["-i", "/state/id", "executor@old", envelope],
        "executor@[2001:db8::7]:22007",
    )
    assert routed[-2:] == ["executor@[2001:db8::7]", envelope]
    assert routed[routed.index("-p") + 1] == "22007"


@pytest.mark.parametrize("target", ["", "host:22", "executor@host", "executor@host:nope"])
def test_admission_route_rejects_incomplete_targets(target: str) -> None:
    with pytest.raises(ValueError):
        policy_ssh._route_argv(
            ["executor@old", '__CBX_EXEC_1__{"v":1,"execution_id":"x"}\ntrue'],
            target,
        )
