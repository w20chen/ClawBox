"""Recognize recorded Tool failures without accepting new replay failures."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .model_gateway import DEFERRED_TOOL_TIMEOUT_PREFIX
from .trace import load_trace


_DEFERRED_TIMEOUT_COMMAND = re.compile(
    rf"\A{re.escape(DEFERRED_TOOL_TIMEOUT_PREFIX)}\d+\n"
)


def _unwrap_deferred_timeout(command: object) -> tuple[object, int]:
    """Return the trace command and lines added by the replay transport."""
    if not isinstance(command, str):
        return command, 0
    match = _DEFERRED_TIMEOUT_COMMAND.match(command)
    if match is None:
        return command, 0
    prefix = match.group(0)
    return command[len(prefix):], prefix.count("\n")


def _remove_wrapper_line_offset(text: str, lines: int) -> str:
    """Undo shell line-number changes caused only by the timeout wrapper."""
    if lines == 0:
        return text

    def shift(match: re.Match[str]) -> str:
        return f"{match[1]}{max(1, int(match[2]) - lines)}{match[3]}"

    return re.sub(r"(?m)^(\S*\bsh: )(\d+)(: )", shift, text)


def _failure(text: str, code: int | None = None) -> tuple[int, str] | None:
    # Research tool imports preserve the original error in the next request.
    directory = re.fullmatch(
        r"Error: Directory not found: ([^\n]+)(?:\n\n\[Analyze the error above and try a different approach\.\])?",
        text.strip(),
    )
    if directory:
        return 1, "missing-directory:" + directory[1]
    exit_match = re.search(r"(?:\A|\n+)(?:Exit code: (\d+)|\(Command exited with code (\d+)\))\s*$", text)
    if exit_match:
        recorded_code = int(exit_match[1] or exit_match[2])
        if code is not None and code != recorded_code:
            return None
        code = recorded_code
        text = text[:exit_match.start()]
    if code is None or code == 0:
        return None
    # The importer translates list_dir into a native Python exec.
    missing = re.search(r"FileNotFoundError: \[Errno 2\] No such file or directory: '([^'\n]+)'\s*$", text)
    if missing:
        return code, "missing-directory:" + missing[1]
    return code, text.strip()


def match_recorded_tool_failures(
    trace: Path, spans: list[dict], *, session_id: str, reported_failures: int,
) -> list[dict]:
    """Require matching call identity, command, exit code, and error text.

    Missing evidence fails closed. This does not mark a benchmark task solved;
    ordinary replay completeness and final validation still apply.
    """
    actions = load_trace(trace)
    expected = {}
    for action, following in zip(actions, actions[1:]):
        calls = action.output.get("tool_calls") or []
        messages = following.input
        assistants = [i for i, item in enumerate(messages)
                      if item.get("role") == "assistant" and item.get("tool_calls")]
        if not assistants:
            continue
        index = assistants[-1]
        original_calls = messages[index]["tool_calls"]
        results = {item.get("tool_call_id"): item.get("content")
                   for item in messages[index + 1:] if item.get("role") == "tool"}
        if len(calls) != len(original_calls):
            continue
        for call, original in zip(calls, original_calls):
            function = call.get("function") or {}
            if function.get("name") != "exec":
                continue
            args = function.get("arguments")
            args = json.loads(args) if isinstance(args, str) else args
            content = results.get(original.get("id"))
            if not isinstance(args, dict) or not isinstance(content, str):
                continue
            failure = _failure(content)
            if failure:
                identity = call.get("id")
                if identity in expected:
                    raise ValueError("recorded Tool call IDs are not unique")
                expected[identity] = (args.get("command"), failure)

    matched = []
    for span in spans:
        if span.get("session_id") != session_id:
            continue
        output = span.get("output") or {}
        code = output.get("exit_code")
        if span.get("status", {}).get("code") == "ok" and code in (None, 0):
            continue
        identity = span.get("span_id")
        result = output.get("result") or {}
        details = result.get("details") or {}
        text = details.get("aggregated")
        if not isinstance(text, str):
            text = "\n".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")
        execution = span.get("execution") or {}
        command = execution.get("requested_command") or execution.get("payload_command")
        command, wrapper_lines = _unwrap_deferred_timeout(command)
        if wrapper_lines:
            text = _remove_wrapper_line_offset(text, wrapper_lines)
        evidence = expected.get(identity)
        if (not evidence or not isinstance(code, int) or code == 0
                or details.get("noOutputTimedOut") or details.get("exitSignal")
                or command != evidence[0] or _failure(text, code) != evidence[1]):
            raise ValueError(f"unexpected replay Tool failure: {identity}")
        matched.append({"call_id": identity, "exit_code": code,
                        "recorded_error": evidence[1][1]})
    # OpenClaw's ``toolSummary.failures`` is a final-answer warning-group
    # signal, not a count of failed Tool spans. One warning can summarize a
    # chain of failed commands. Every observed failed span is matched above;
    # require evidence on both sides without equating their cardinalities.
    if reported_failures < 1 or not matched:
        raise ValueError("Runtime Tool failure evidence does not cover OpenClaw failure signal")
    return matched
