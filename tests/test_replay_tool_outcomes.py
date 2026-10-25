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


def test_deferred_timeout_wrapper_preserves_recorded_failure_identity(monkeypatch):
    command = "cd /testbed && python -m pytest tests/ -v 2>&1"
    span = setup_case(
        monkeypatch,
        "/bin/sh: 1: python: not found\n\nExit code: 127",
        "/bin/sh: 2: python: not found",
        command=command,
    )
    span["execution"]["requested_command"] = (
        "CLAWBOX_DEFERRED_TOOL_TIMEOUT_SECONDS=60\n" + command
    )
    span["output"]["exit_code"] = 127

    result = tool_outcomes.match_recorded_tool_failures(
        Path("trace"), [span], session_id="s", reported_failures=1,
    )

    assert result == [{
        "call_id": "call_0_0",
        "exit_code": 127,
        "recorded_error": "/bin/sh: 1: python: not found",
    }]


def test_recorded_empty_output_nonzero_exit(monkeypatch):
    span = setup_case(monkeypatch, "Exit code: 1", "\n\n(Command exited with code 1)",
                      command='grep missing /testbed/file')
    assert tool_outcomes.match_recorded_tool_failures(
        Path('trace'), [span], session_id='s', reported_failures=1,
    ) == [{'call_id': 'call_0_0', 'exit_code': 1, 'recorded_error': ''}]


def test_imported_directory_error(monkeypatch):
    span = setup_case(monkeypatch,
        "Error: Directory not found: /testbed/missing\n\n[Analyze the error above and try a different approach.]",
        "Traceback (most recent call last):\nFileNotFoundError: [Errno 2] No such file or directory: '/testbed/missing'\n\n(Command exited with code 1)")
    assert tool_outcomes.match_recorded_tool_failures(Path("trace"), [span], session_id="s", reported_failures=1)


@pytest.mark.parametrize("mutation", ["new_error", "wrong_command", "wrong_call", "timeout", "missing_span", "wrong_exit", "original_success"])
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
            session_id="s", reported_failures=1)


def test_openclaw_failure_summary_is_a_signal_not_a_span_count(monkeypatch):
    error = "TypeError: bug"
    first = setup_case(
        monkeypatch, error + "\nExit code: 1",
        error + "\n(Command exited with code 1)",
    )
    second = json.loads(json.dumps(first))
    second["span_id"] = "call_0_1"
    actions = tool_outcomes.load_trace(Path("trace"))
    actions[0].output["tool_calls"].append({
        "id": "call_0_1",
        "function": {
            "name": "exec",
            "arguments": json.dumps({"command": "python3 reproduce.py"}),
        },
    })
    actions[1].input[0]["tool_calls"].append({"id": "original-id-2"})
    actions[1].input.append({
        "role": "tool",
        "tool_call_id": "original-id-2",
        "content": error + "\nExit code: 1",
    })
    monkeypatch.setattr(tool_outcomes, "load_trace", lambda _: actions)

    result = tool_outcomes.match_recorded_tool_failures(
        Path("trace"), [first, second], session_id="s", reported_failures=1,
    )

    assert [item["call_id"] for item in result] == ["call_0_0", "call_0_1"]
