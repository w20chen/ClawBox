"""ClawTune-compatible KB snapshot adapter (P1/P2).

The runtime pod's ClawTune sidecar loads a KB snapshot through
``RuntimeToolResourceKB.from_json_obj`` (schema
``runtime_tool_resource_kb_v1``).  The projector must therefore be able to
rebuild that ClawTune-loadable snapshot from the trusted
``ToolObservation``s, without depending on the ClawTune Python package (it
lives in a separate repo/container).

This module replicates the parts of ClawTune's
``services/sidecar/src/tool_resource/runtime_kb.py`` and
``tool_time/command.py`` that determine the snapshot shape:

* ``CompletedCall`` — the call-level record the KB accumulates.
* the normalized command token stream / heads (env-assignment stripping,
  redirection dropping, path basename on heads, separator tokens kept).
* the node-key layout (repo exact_command / command_prefix_depth_N /
  binary_head / tool_name, public binary_head / tool_name / global).
* the target values (latency_ms from the span interval; peak_cpu_cores;
  peak_memory_mb stored as a residual — with a zero ambient anchor so the
  observed absolute peak RSS is what lands in the node).

The output JSON is loadable by the sidecar's ``from_json_obj``; predictions
made from the loaded snapshot use the sidecar's own query-time tokenizer, so
stored node keys only need to be structurally consistent with what the
sidecar produces for the same command text.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone
from typing import Any

from .schema import ToolObservation

_SCHEMA = "runtime_tool_resource_kb_v1"
_QUANTILE = 0.9
_MAX_PREFIX_DEPTH = 4
_TARGETS = ("latency_ms", "peak_cpu_cores", "peak_memory_mb")

_HEAD_SEPARATOR_CHARS = frozenset(";|&()")
_REDIRECTION_CHARS = frozenset("<>&")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def to_epoch(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


# ── command tokenization (mirrors tool_time/command.py) ────────────────

def _is_redirection_operator(token: str) -> bool:
    return (
        bool(token)
        and all(char in _REDIRECTION_CHARS for char in token)
        and any(char in "<>" for char in token)
    )


def _normalized_tokens(command: str) -> tuple[tuple[str, bool], ...]:
    """Normalized ``(token, is_head)`` stream, mirroring ClawTune's lexer."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return ()
    result: list[tuple[str, bool]] = []
    expect_head = True
    skip_redirection_target = False
    for index, token in enumerate(tokens):
        if not token:
            continue
        if all(char in _HEAD_SEPARATOR_CHARS for char in token):
            expect_head = True
            skip_redirection_target = False
            result.append((token, False))
            continue
        if _is_redirection_operator(token):
            skip_redirection_target = True
            continue
        if skip_redirection_target:
            skip_redirection_target = False
            continue
        if (
            token.isdigit()
            and index + 1 < len(tokens)
            and _is_redirection_operator(tokens[index + 1])
            and "&" in tokens[index + 1]
        ):
            continue
        if expect_head:
            if _ENV_ASSIGNMENT.match(token):
                continue
            head = token.rsplit("/", 1)[-1]
            if head:
                result.append((head, True))
                expect_head = False
            continue
        result.append((token, False))
    return tuple(result)


def shell_command_heads(command: str | None) -> list[str]:
    if not command:
        return []
    return [token for token, is_head in _normalized_tokens(command) if is_head]


def shell_command_prefix_tokens(command: str | None) -> list[str]:
    if not command:
        return []
    return [token for token, _ in _normalized_tokens(command)]


def _single_head(command: str | None) -> str | None:
    if not isinstance(command, str) or not command.strip():
        return None
    heads = shell_command_heads(command)
    return heads[0] if len(heads) == 1 else None


# ── CompletedCall adaptation ───────────────────────────────────────────

def observation_to_completed_call(
    observation: ToolObservation,
    repo: str,
) -> dict[str, Any]:
    """Adapt a trusted ToolObservation into a ClawTune ``CompletedCall`` dict.

    Memory is stored as an absolute-peak-RSS residual against a zero ambient
    anchor so the node receives the observed peak (the runtime KB semantics
    keep residual values and add back the query ambient at prediction time).
    """
    end = observation.end_time
    start = observation.start_time or end
    if end is None or start is None:
        end_epoch = to_epoch(observation.created_at)
        start_epoch = end_epoch - (observation.duration_sec or 0.0)
    else:
        start_epoch = to_epoch(start)
        end_epoch = to_epoch(end)
    censored = not observation.complete or observation.exit_code != 0
    cpu_cores = observation.cpu_utilization_avg_cores
    rss_bytes = observation.rss_peak_bytes
    return {
        "repo": repo,
        "tool_name": observation.tool_name,
        "command": observation.command,
        "ts_start": round(start_epoch, 6),
        "ts_end": round(end_epoch, 6),
        "censored": censored,
        "peak_cpu_cores": float(cpu_cores) if cpu_cores is not None else None,
        "peak_cpu_cores_eligible": cpu_cores is not None,
        "peak_memory_mb": (float(rss_bytes) / (1024.0 * 1024.0)) if rss_bytes is not None else None,
        "peak_memory_mb_eligible": rss_bytes is not None,
        "ambient_before_mb": 0.0 if rss_bytes is not None else None,
    }


def _target_values(call: dict[str, Any]) -> dict[str, float]:
    values: dict[str, float] = {}
    if not call["censored"]:
        values["latency_ms"] = (call["ts_end"] - call["ts_start"]) * 1000.0
    if call["peak_cpu_cores_eligible"] and call["peak_cpu_cores"] is not None:
        values["peak_cpu_cores"] = float(call["peak_cpu_cores"])
    if (
        call["peak_memory_mb_eligible"]
        and call["peak_memory_mb"] is not None
        and call["ambient_before_mb"] is not None
    ):
        values["peak_memory_mb"] = float(call["peak_memory_mb"]) - float(call["ambient_before_mb"])
    return values


# ── node keys (mirrors runtime_kb.py backoff layout) ───────────────────

def _repo_keys(tool_name: str, command: str | None) -> list[tuple[str, str]]:
    if command is None:
        return [("tool_name", tool_name)] if tool_name else []
    if not isinstance(command, str) or not command.strip():
        return []
    tokens = shell_command_prefix_tokens(command)
    if not tokens:
        return []
    keys: list[tuple[str, str]] = [("exact_command", " ".join(tokens))]
    depth = min(len(tokens), _MAX_PREFIX_DEPTH)
    for length in range(depth, 0, -1):
        keys.append((f"command_prefix_depth_{length}", " ".join(tokens[:length])))
    head = _single_head(command)
    if head is not None:
        keys.append(("binary_head", head))
    return keys


def _public_keys(tool_name: str, command: str | None) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    head = _single_head(command)
    if head is not None:
        keys.append(("binary_head", head))
    keys.append(("tool_name", tool_name))
    keys.append(("global", ""))
    return keys


# ── snapshot builder ───────────────────────────────────────────────────

def _nodes_to_json(nodes: dict[tuple[str, str], list[float]]) -> list[list[Any]]:
    return [[kind, key, list(values)] for (kind, key), values in nodes.items()]


def build_clawtune_kb_snapshot(
    observations: list[ToolObservation],
    repo: str,
) -> dict[str, Any]:
    """Build a ``RuntimeToolResourceKB.to_json_obj``-shaped snapshot.

    The repo layer is accumulated from all trusted observations; the public
    layer is fitted from the same corpus (the control plane has no separate
    cross-tenant public corpus yet, so cold-start within a tenant is all we
    can promise).
    """
    calls = [observation_to_completed_call(obs, repo) for obs in observations]
    public: dict[str, dict[tuple[str, str], list[float]]] = {
        target: {} for target in _TARGETS
    }
    repo_nodes: dict[str, dict[str, dict[tuple[str, str], list[float]]]] = {}
    for call in calls:
        values = _target_values(call)
        for key in _public_keys(call["tool_name"], call["command"]):
            for target, value in values.items():
                public[target].setdefault(key, []).append(value)
        repo_layer = repo_nodes.setdefault(
            call["repo"], {target: {} for target in _TARGETS}
        )
        for key in _repo_keys(call["tool_name"], call["command"]):
            for target, value in values.items():
                repo_layer[target].setdefault(key, []).append(value)
    return {
        "schema": _SCHEMA,
        "quantile": _QUANTILE,
        "max_prefix_depth": _MAX_PREFIX_DEPTH,
        "public": {
            target: _nodes_to_json(nodes) for target, nodes in public.items()
        },
        "repo": {
            repo_key: {
                target: _nodes_to_json(nodes) for target, nodes in targets.items()
            }
            for repo_key, targets in repo_nodes.items()
        },
        "pending": [],
        "last_query_ts": None,
    }
