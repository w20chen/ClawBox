import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawbox.replay import tool_outcomes


def setup_case(monkeypatch, recorded, actual, *, command="python3 reproduce.py"):
    calls = [{"id": "call_0_0", "function": {"name": "exec", "arguments": json.dumps({"command": command})}}]
    actions = [SimpleNamespace(output={"tool_calls": calls}), SimpleNamespace(input=[
        {"role": "assistant", "tool_calls": [{"id": "original-id"}]},
        {"role": "tool", "tool_call_id": "original-id", "content": recorded},
    ])]
    monkeypatch.setattr(tool_outcomes, "load_trace", lambda _: actions)
    span = {"session_id": "s", "span_id": "call_0_0", "status": {"code": "error"},
            "execution": {"requested_command": command},
            "output": {"exit_code": 1, "result": {"details": {"aggregated": actual}}}}
    return span


@pytest.mark.parametrize("error", ["TypeError: 'module' object is not callable", "ModuleNotFoundError: No module named 'six'"])
def test_recorded_nonzero_is_preserved(monkeypatch, error):
    span = setup_case(monkeypatch, error + "\n\nExit code: 1", error + "\n\n(Command exited with code 1)")
    result = tool_outcomes.match_recorded_tool_failures(Path("trace"), [span], session_id="s", reported_failures=1)
    assert result == [{"call_id": "call_0_0", "exit_code": 1, "recorded_error": error}]


def test_imported_directory_error(monkeypatch):
    span = setup_case(monkeypatch,
        "Error: Directory not found: /testbed/missing\n\n[Analyze the error above and try a different approach.]",
        "Traceback (most recent call last):\nFileNotFoundError: [Errno 2] No such file or directory: '/testbed/missing'\n\n(Command exited with code 1)")
    assert tool_outcomes.match_recorded_tool_failures(Path("trace"), [span], session_id="s", reported_failures=1)


@pytest.mark.parametrize("mutation", ["new_error", "wrong_command", "wrong_call", "timeout", "missing_span", "count_mismatch", "wrong_exit", "original_success"])
def test_new_failures_are_not_hidden(monkeypatch, mutation):
    recorded = "done" if mutation == "original_success" else "TypeError: bug\nExit code: 1"
    span = setup_case(monkeypatch, recorded, "TypeError: bug\n(Command exited with code 1)")
    if mutation == "new_error": span["output"]["result"]["details"]["aggregated"] = "ModuleNotFoundError: new\n(Command exited with code 1)"
    if mutation == "wrong_command": span["execution"]["requested_command"] = "different command"
    if mutation == "wrong_call": span["span_id"] = "unknown"
    if mutation == "timeout": span["output"]["result"]["details"]["noOutputTimedOut"] = True
    if mutation == "wrong_exit": span["output"]["exit_code"] = 2
    with pytest.raises(ValueError):
        tool_outcomes.match_recorded_tool_failures(Path("trace"), [] if mutation == "missing_span" else [span],
            session_id="s", reported_failures=2 if mutation == "count_mismatch" else 1)
