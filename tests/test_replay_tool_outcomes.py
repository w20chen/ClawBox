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


def test_imported_directory_error_does_not_override_actual_exit_code(monkeypatch):
    span = setup_case(monkeypatch, "Error: Directory not found: /testbed/missing",
                      "Error: Directory not found: /testbed/missing")
    span["output"]["exit_code"] = 2
    with pytest.raises(ValueError, match="unexpected replay Tool failure"):
        tool_outcomes.match_recorded_tool_failures(Path("trace"), [span], session_id="s", reported_failures=1)


@pytest.mark.parametrize("mutation", ["new_error", "wrong_command", "wrong_call", "timeout", "signal", "missing_span", "wrong_exit", "original_success", "preflight"])
def test_new_failures_are_not_hidden(monkeypatch, mutation):
    recorded = "done" if mutation == "original_success" else "TypeError: bug\nExit code: 1"
    span = setup_case(monkeypatch, recorded, "TypeError: bug\n(Command exited with code 1)")
    if mutation == "new_error": span["output"]["result"]["details"]["aggregated"] = "ModuleNotFoundError: new\n(Command exited with code 1)"
    if mutation == "wrong_command": span["execution"]["requested_command"] = "different command"
    if mutation == "wrong_call": span["span_id"] = "unknown"
    if mutation == "timeout": span["output"]["result"]["details"]["noOutputTimedOut"] = True
    if mutation == "signal": span["output"]["result"]["details"]["exitSignal"] = "SIGKILL"
    if mutation == "wrong_exit": span["output"]["exit_code"] = 2
    if mutation == "preflight":
        span["output"].update(exit_code=0)
        span["output"]["result"]["details"].update(aggregated="exec preflight: refusing to run", status="error")
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


@pytest.mark.parametrize("recorded,actual,command,code", [
    (
        "lrwxrwxrwx 1 root root 10 Apr  9  2023 /usr/bin/python3 -> python3.11\n"
        "-rwxr-xr-x 1 root root 6839928 May  2  2024 /usr/bin/python3.11",
        "lrwxrwxrwx. 1 root root 10 Apr  9  2023 /usr/bin/python3 -> python3.11\n"
        "-rwxr-xr-x. 1 root root 6618336 May  2  2024 /usr/bin/python3.11",
        "ls -la /usr/bin/python* /usr/local/bin/python* 2>/dev/null", 2,
    ),
    (
        "platform linux -- Python 3.11.2, pytest-9.1.1, pluggy-1.6.0 -- /usr/bin/python3\n"
        "E   AttributeError: module 'collections' has no attribute 'MutableMapping'\n"
        "=============================== 1 error in 0.11s ===============================",
        "platform linux -- Python 3.11.2, pytest-9.2.0, pluggy-1.7.0 -- /usr/bin/python3\n"
        "E   AttributeError: module 'collections' has no attribute 'MutableMapping'\n"
        "=============================== 1 error in 0.36s ===============================",
        "cd /testbed && python3 -m pytest tests/test_deprecation.py -v 2>&1", 2,
    ),
    ("Ran 51 tests in 0.019s\nFAILED (errors=1)",
     "Ran 51 tests in 0.343s\nFAILED (errors=1)", "python3 -m unittest", 1),
    ("file.py:1: trailing whitespace.\n+    foo\r", "file.py:1: trailing whitespace.\r\n+    foo\r\n",
     "git diff --check", 2),
    ("ImportError: symbol (/tmp/foo.cpython-311-x86_64-linux-gnu.so)",
     "ImportError: symbol (/tmp/foo.cpython-311-aarch64-linux-gnu.so)", "python3 reproduce.py", 1),
    ("retained prefix\n\n... (14 chars truncated) ...\n\nretained suffix",
     "retained prefix\noriginal text\nretained suffix", "sed -n '1,100p' file; grep absent file", 1),
])
def test_recorded_failure_presentation_fields(monkeypatch, recorded, actual, command, code):
    span = setup_case(monkeypatch, recorded + f"\nExit code: {code}", actual, command=command)
    span["output"]["exit_code"] = code
    assert tool_outcomes.match_recorded_tool_failures(
        Path("trace"), [span], session_id="s", reported_failures=1,
    )


@pytest.mark.parametrize("actual", [
    "Ran 51 tests in 0.343s\nFAILED (errors=2)",
    "Ran 50 tests in 0.343s\nFAILED (errors=1)",
    "ModuleNotFoundError: No module named 'pytest'",
])
def test_presentation_normalization_preserves_changed_outcomes(monkeypatch, actual):
    span = setup_case(monkeypatch, "Ran 51 tests in 0.019s\nFAILED (errors=1)\nExit code: 1", actual)
    with pytest.raises(ValueError, match="unexpected replay Tool failure"):
        tool_outcomes.match_recorded_tool_failures(Path("trace"), [span], session_id="s", reported_failures=1)


def test_pytest_failure_order_and_object_addresses_are_presentation_only(monkeypatch):
    recorded = (
        "E   TypeError: bad <Thing object at 0x1234>\n"
        "FAILED tests/test_b.py::test_b - TypeError: bad <Thing object at 0x1234>\n"
        "FAILED tests/test_a.py::test_a - AssertionError: no\n"
        "================ 2 failed, 7 passed in 0.35s ================\nExit code: 1"
    )
    actual = (
        "E   TypeError: bad <Thing object at 0x9abc>\n"
        "FAILED tests/test_a.py::test_a - AssertionError: no\n"
        "FAILED tests/test_b.py::test_b - TypeError: bad <Thing object at 0x9abc>\n"
        "================ 2 failed, 7 passed in 2.87s ================"
    )
    span = setup_case(monkeypatch, recorded, actual, command="python -m pytest")
    assert tool_outcomes.match_recorded_tool_failures(
        Path("trace"), [span], session_id="s", reported_failures=1,
    )


def test_pytest_same_counts_do_not_hide_changed_failure(monkeypatch):
    recorded = (
        "FAILED tests/test_a.py::test_a - AssertionError: old\n"
        "================ 1 failed, 7 passed in 0.35s ================\nExit code: 1"
    )
    actual = (
        "FAILED tests/test_a.py::test_a - AssertionError: new\n"
        "================ 1 failed, 7 passed in 2.87s ================"
    )
    span = setup_case(monkeypatch, recorded, actual, command="python -m pytest")
    with pytest.raises(ValueError, match="unexpected replay Tool failure"):
        tool_outcomes.match_recorded_tool_failures(
            Path("trace"), [span], session_id="s", reported_failures=1,
        )


def test_truncated_randomized_pytest_failure_keeps_retained_evidence(monkeypatch):
    command = "python -m pytest tests/test_to_thread.py -v"
    recorded = (
        "tests/test_to_thread.py::test_cancelled[trio] FAILED [ 20%]\n"
        "\n... (12000 chars truncated) ...\n\n"
        "    | TypeError: run_sync() got an unexpected keyword argument 'cancellable'\n"
        "================ 2 failed, 8 passed in 4.23s ================\n"
        "Exit code: 1"
    )
    actual = (
        "tests/test_to_thread.py::test_custom[asyncio] PASSED [ 10%]\n"
        "tests/test_to_thread.py::test_other[trio] FAILED [ 20%]\n"
        "tests/test_to_thread.py::test_cancelled[trio] FAILED [ 30%]\n"
        "    | TypeError: run_sync() got an unexpected keyword argument 'cancellable'\n"
        "================ 2 failed, 8 passed in 18.76s ================"
    )
    span = setup_case(monkeypatch, recorded, actual, command=command)

    assert tool_outcomes.match_recorded_tool_failures(
        Path("trace"), [span], session_id="s", reported_failures=1,
    )


@pytest.mark.parametrize("mutation", ["count", "exception", "retained_test"])
def test_truncated_randomized_pytest_failure_rejects_changed_evidence(monkeypatch, mutation):
    command = "python -m pytest tests/test_to_thread.py -v"
    recorded = (
        "tests/test_to_thread.py::test_cancelled[trio] FAILED [ 20%]\n"
        "\n... (12000 chars truncated) ...\n\n"
        "    | TypeError: old failure\n"
        "================ 2 failed, 8 passed in 4.23s ================\n"
        "Exit code: 1"
    )
    actual = (
        "tests/test_to_thread.py::test_other[trio] FAILED [ 20%]\n"
        "tests/test_to_thread.py::test_cancelled[trio] FAILED [ 30%]\n"
        "    | TypeError: old failure\n"
        "================ 2 failed, 8 passed in 18.76s ================"
    )
    if mutation == "count":
        actual = actual.replace("2 failed, 8 passed", "3 failed, 7 passed")
    elif mutation == "exception":
        actual = actual.replace("TypeError: old failure", "TypeError: new failure")
    else:
        actual = actual.replace("test_cancelled[trio]", "test_replacement[trio]")
    span = setup_case(monkeypatch, recorded, actual, command=command)

    with pytest.raises(ValueError, match="unexpected replay Tool failure"):
        tool_outcomes.match_recorded_tool_failures(
            Path("trace"), [span], session_id="s", reported_failures=1,
        )


def test_generation_host_path_inventory_keeps_task_pytest_identity(monkeypatch):
    command = (
        'which pytest; find / -name pytest -type f 2>/dev/null | head -5; '
        'python3 -c "import pytest" 2>&1'
    )
    recorded = (
        "/home/generator/.venv/bin/pytest\n"
        "/opt/conda/envs/testbed/bin/pytest\n"
        "/opt/conda/pkgs/pytest/bin/pytest\n"
        "ModuleNotFoundError: No module named 'pytest'\nExit code: 1"
    )
    actual = (
        "/opt/conda/envs/testbed/bin/pytest\n"
        "/opt/clawtune/cache/go/src/net/pipe.go\n"
        "ModuleNotFoundError: No module named 'pytest'"
    )
    span = setup_case(monkeypatch, recorded, actual, command=command)
    assert tool_outcomes.match_recorded_tool_failures(
        Path("trace"), [span], session_id="s", reported_failures=1,
    )

    span = setup_case(
        monkeypatch, recorded,
        "/opt/clawtune/cache/go/src/net/pipe.go\nModuleNotFoundError: No module named 'pytest'",
        command=command,
    )
    with pytest.raises(ValueError, match="unexpected replay Tool failure"):
        tool_outcomes.match_recorded_tool_failures(
            Path("trace"), [span], session_id="s", reported_failures=1,
        )


def test_find_listing_order_and_host_extras_are_presentation_only(monkeypatch):
    command = "find /testbed -maxdepth 2 -type f; echo ---; cat setup.cfg"
    recorded = "/testbed/setup.py\n/testbed/pkg/a.py\n---\n[metadata]\nExit code: 1"
    actual = "/testbed/pkg/a.py\n/testbed/setup.py\n---\n[metadata]"
    span = setup_case(monkeypatch, recorded, actual, command=command)
    assert tool_outcomes.match_recorded_tool_failures(
        Path("trace"), [span], session_id="s", reported_failures=1,
    )

    command = "find / -name 'pipe*' 2>/dev/null"
    recorded = "/usr/lib/jvm/java-17-openjdk-amd64/pipe.md\n/usr/lib/python3.11/pipes.py\nExit code: 1"
    actual = (
        "/usr/src/linux/drivers/pipe.c\n"
        "/usr/lib/python3.11/pipes.py\n"
        "/usr/lib/jvm/java-17-openjdk-arm64/pipe.md"
    )
    span = setup_case(monkeypatch, recorded, actual, command=command)
    assert tool_outcomes.match_recorded_tool_failures(
        Path("trace"), [span], session_id="s", reported_failures=1,
    )


def test_find_listing_does_not_hide_missing_task_path(monkeypatch):
    command = "find /testbed -maxdepth 2 -type f"
    span = setup_case(
        monkeypatch, "/testbed/setup.py\n/testbed/pkg/a.py\nExit code: 1",
        "/testbed/pkg/a.py", command=command,
    )
    with pytest.raises(ValueError, match="unexpected replay Tool failure"):
        tool_outcomes.match_recorded_tool_failures(
            Path("trace"), [span], session_id="s", reported_failures=1,
        )


def test_pytest_short_summary_elapsed_time_is_presentation_only(monkeypatch):
    command = "python -m pytest tests/test_lineage.py -q"
    recorded = (
        "FAILED tests/test_lineage.py::test_name - AssertionError: same\n"
        "1 failed in 0.16s\nExit code: 1"
    )
    actual = (
        "FAILED tests/test_lineage.py::test_name - AssertionError: same\n"
        "1 failed in 0.24s"
    )
    span = setup_case(monkeypatch, recorded, actual, command=command)
    assert tool_outcomes.match_recorded_tool_failures(
        Path("trace"), [span], session_id="s", reported_failures=1,
    )


def test_truncated_recording_cannot_hide_a_new_exception(monkeypatch):
    span = setup_case(monkeypatch, "prefix\n\n... (50 chars truncated) ...\n\nsuffix\nExit code: 1",
                      "prefix\nModuleNotFoundError: new\nsuffix")
    with pytest.raises(ValueError, match="unexpected replay Tool failure"):
        tool_outcomes.match_recorded_tool_failures(Path("trace"), [span], session_id="s", reported_failures=1)
