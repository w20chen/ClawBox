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
    text = text.replace("\r\n", "\n")
    # Research tool imports preserve the original error in the next request.
    directory = re.fullmatch(
        r"Error: Directory not found: ([^\n]+)(?:\n\n\[Analyze the error above and try a different approach\.\])?",
        text.strip(),
    )
    if directory:
        return (1, "missing-directory:" + directory[1]) if code in (None, 1) else None
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


def _comparison_text(text: str, command: str) -> str:
    """Remove presentation fields, retaining diagnostics and test outcomes."""
    text = text.replace("\r\n", "\n")
    text = re.sub(r"(?m)^(Ran \d+ tests? in )\d+(?:\.\d+)?s$", r"\1<elapsed>s", text)
    text = re.sub(
        r"(?m)^(=+ [^\n]+ in )\d+(?:\.\d+)?s( =+)$", r"\1<elapsed>s\2", text,
    )
    text = re.sub(
        r"(?m)^((?:\d+ (?:failed|passed|errors?|warnings?|skipped|xfailed|xpassed))"
        r"(?:, \d+ (?:failed|passed|errors?|warnings?|skipped|xfailed|xpassed))* in )"
        r"\d+(?:\.\d+)?s$",
        r"\1<elapsed>s", text,
    )
    # A package banner is not the exception. Changed failures below it still
    # have to agree, including their traceback, test counts, and exit code.
    text = re.sub(r"(?m)^(platform [^\n]*, pytest-)\S+(, pluggy-)\S+", r"\1<version>\2<version>", text)
    text = re.sub(r"(?m)^Using --randomly-seed=\d+\n?", "", text)
    text = re.sub(
        r"(?m)^plugins: (.+)$",
        lambda match: "plugins: " + ", ".join(sorted(match[1].split(", "))), text,
    )
    text = re.sub(r"(\.cpython-\d+-)(?:x86_64|aarch64)(-linux-gnu\.so)", r"\1<arch>\2", text)
    # Default object reprs expose process-specific addresses without changing
    # the exception or tested outcome.
    text = re.sub(r"(?<= at )0x[0-9a-fA-F]+", "<address>", text)
    if command.lstrip().startswith("ls "):
        # Preserve path, link target, permission bits, owner and group. File
        # sizes/date and the SELinux indicator vary across native task images.
        text = re.sub(
            r"(?m)^([bcdlps-][rwxStTs-]{9})\.?\s+(\d+\s+\S+\s+\S+)\s+\d+\s+"
            r"[A-Z][a-z]{2}\s+\d+\s+[\d:]+\s+(.+)$",
            r"\1 \2 <size> <date> \3", text,
        )
    if "git status --short" in command:
        text = re.sub(r"(?m)^\?\? openclaw-ssh-shared-[0-9a-f]+/\n?", "", text)
    if re.search(r"(?:^|[;&|]\s*)find\s+/\s", command) and "pytest" in command:
        # These two recorded diagnostics searched the whole generation host and
        # printed unrelated runner/cache files. Retain the task interpreter's
        # pytest path and every non-path diagnostic; later replay calls execute
        # this exact /opt/conda path.
        text = "\n".join(
            line for line in text.splitlines()
            if not line.startswith("/")
            or line == "/opt/conda/envs/testbed/bin/pytest"
        )
    return text.strip()


def _find_listing_equivalent(left: str, right: str, command: str) -> bool:
    """Compare find output without treating path order as command failure."""
    if not re.search(r"(?:^|[;&|]\s*)find\s+/\S*", command):
        return False

    def split(text: str) -> tuple[set[str], list[str]]:
        paths: set[str] = set()
        diagnostics: list[str] = []
        for line in text.splitlines():
            if line.startswith("/"):
                paths.add(re.sub(
                    r"(?i)(?<![\w])(?:amd64|x86_64|aarch64|arm64)(?![\w])",
                    "<arch>", line,
                ))
            else:
                diagnostics.append(line)
        return paths, diagnostics

    expected_paths, expected_diagnostics = split(left)
    actual_paths, actual_diagnostics = split(right)
    if expected_diagnostics != actual_diagnostics or not expected_paths:
        return False
    whole_host = bool(re.search(r"(?:^|[;&|]\s*)find\s+/\s", command))
    return (
        expected_paths.issubset(actual_paths)
        if whole_host else expected_paths == actual_paths
    )


def _pytest_outcome_signature(text: str) -> tuple[tuple[tuple[str, int], ...], tuple[str, ...]] | None:
    """Return order-insensitive pytest failures while retaining their detail."""
    summaries = [
        line for line in text.splitlines()
        if re.match(r"^=+ .+ =+$", line)
        and re.search(r"\b\d+ (?:failed|passed|errors?|warnings?|skipped|xfailed|xpassed)\b", line)
    ]
    if not summaries:
        return None
    counts: dict[str, int] = {}
    for count, kind in re.findall(
        r"\b(\d+) (failed|passed|errors?|warnings?|skipped|xfailed|xpassed)\b",
        summaries[-1],
    ):
        counts[{"errors": "error", "warnings": "warning"}.get(kind, kind)] = int(count)
    if not counts or not (counts.get("failed", 0) or counts.get("error", 0)):
        return None
    outcomes = tuple(sorted(re.findall(r"(?m)^(?:FAILED|ERROR)\s+[^\n]+$", text)))
    if not outcomes:
        return None
    return tuple(sorted(counts.items())), outcomes


def _pytest_failure_counts(text: str) -> tuple[tuple[str, int], ...] | None:
    """Return the final pytest counts even when its short summary was truncated."""
    summaries = [
        line for line in text.splitlines()
        if re.match(r"^=+ .+ =+$", line)
        and re.search(r"\b\d+ (?:failed|passed|errors?|warnings?|skipped|xfailed|xpassed)\b", line)
    ]
    if not summaries:
        return None
    counts = {
        {"errors": "error", "warnings": "warning"}.get(kind, kind): int(count)
        for count, kind in re.findall(
            r"\b(\d+) (failed|passed|errors?|warnings?|skipped|xfailed|xpassed)\b",
            summaries[-1],
        )
    }
    if not (counts.get("failed", 0) or counts.get("error", 0)):
        return None
    return tuple(sorted(counts.items()))


def _truncated_pytest_failure_equivalent(left: str, right: str) -> bool:
    """Match randomized pytest failures using only evidence retained in a trace."""
    if _pytest_failure_counts(left) != _pytest_failure_counts(right):
        return False
    retained_failures = set(re.findall(
        r"(?m)^([^\n]+::[^\n]+?)\s+FAILED\s+\[\s*\d+%\]$", left,
    ))
    replay_failures = set(re.findall(
        r"(?m)^([^\n]+::[^\n]+?)\s+FAILED\s+\[\s*\d+%\]$", right,
    ))
    if not retained_failures or not retained_failures.issubset(replay_failures):
        return False
    exception_lines = re.compile(
        r"(?m)^\s*(?:E\s+|\|\s+|\+\s+)?"
        r"([A-Za-z_][\w.]+(?:Error|Exception|Warning)(?::[^\n]*)?)\s*$"
    )
    expected_exceptions = set(exception_lines.findall(left))
    return bool(expected_exceptions) and expected_exceptions == set(exception_lines.findall(right))


def _equivalent_failure(expected: tuple[int, str], actual: tuple[int, str] | None,
                        command: str) -> bool:
    if actual is None or actual[0] != expected[0]:
        return False
    left, right = (_comparison_text(value[1], command) for value in (expected, actual))
    if left == right:
        return True
    if _find_listing_equivalent(left, right, command):
        return True
    # pytest-randomly and parallel collection can reorder progress, tracebacks,
    # and the final FAILED lines. Accept only an identical final count and the
    # identical detailed FAILED/ERROR lines as an unordered set.
    left_pytest = _pytest_outcome_signature(left)
    if left_pytest is not None and left_pytest == _pytest_outcome_signature(right):
        return True
    # Research recordings may omit a middle block. Match both retained ends;
    # never use a truncation marker to excuse a new exception in the output.
    marker = re.search(r"\n\n\.\.\. \(\d+ chars truncated\) \.\.\.\n\n", left)
    if marker is None or re.search(r"chars truncated", left[marker.end():]):
        return False
    if _truncated_pytest_failure_equivalent(left, right):
        return True
    diagnostics = r"(?m)^(?:E\s+)?(?:[\w.]+(?:Error|Exception|Warning)|FAILED|ERROR|FAIL:)[^\n]*"
    if re.findall(diagnostics, left) != re.findall(diagnostics, right):
        return False
    return right.startswith(left[:marker.start()]) and right.endswith(left[marker.end():])


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
        if (not evidence or type(code) is not int or code == 0
                or details.get("noOutputTimedOut") or details.get("exitSignal")
                or command != evidence[0]
                or not _equivalent_failure(evidence[1], _failure(text, code), command)):
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
