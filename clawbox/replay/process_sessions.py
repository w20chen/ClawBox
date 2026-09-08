"""Bind recorded OpenClaw background-session handles to the current run.

Only identities emitted by matching tool calls establish a binding. OpenClaw
still launches, polls and cancels every process; this module executes no tools.
"""
from __future__ import annotations

import re
from typing import Any


_RUNNING = re.compile(r"Command still running \(session ([\w-]+), pid ([0-9]+)\)")


def bind_process_sessions(expected: Any, actual: Any,
                          bindings: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    result = dict(bindings)
    if not isinstance(expected, list) or not isinstance(actual, list):
        return result
    observed = {message.get("tool_call_id"): message for message in actual
                if isinstance(message, dict) and message.get("role") == "tool"}
    for message in expected:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        counterpart = observed.get(message.get("tool_call_id"))
        if counterpart is None:
            continue
        before, after = message.get("content"), counterpart.get("content")
        if not isinstance(before, str) or not isinstance(after, str):
            continue
        old, new = _RUNNING.search(before), _RUNNING.search(after)
        if old is None or new is None:
            continue
        binding = {"session_id": new[1], "pid": new[2], "recorded_pid": old[2]}
        if old[1] in result and result[old[1]] != binding:
            raise ValueError("OpenClaw background session changed identity within one run")
        if any(key != old[1] and value["session_id"] == new[1]
               for key, value in result.items()):
            raise ValueError("two recorded background sessions map to one process")
        result[old[1]] = binding
    return result


def rebind_process_sessions(value: Any, bindings: dict[str, dict[str, str]]) -> Any:
    """Return a translated copy; never modify the recording or command contents otherwise."""
    if isinstance(value, list):
        return [rebind_process_sessions(item, bindings) for item in value]
    if isinstance(value, dict):
        return {key: rebind_process_sessions(item, bindings) for key, item in value.items()}
    if not isinstance(value, str) or not bindings:
        return value

    def report(match):
        binding = bindings.get(match[1])
        if binding is None:
            return match[0]
        return f"Command still running (session {match[1]}, pid {binding['pid']})"

    value = _RUNNING.sub(report, value)
    # Replace simultaneously so two handles cannot accidentally cascade.
    pattern = r"(?<![\w-])(" + "|".join(re.escape(key) for key in bindings) + r")(?![\w-])"
    return re.sub(pattern, lambda match: bindings[match[0]]["session_id"], value)
