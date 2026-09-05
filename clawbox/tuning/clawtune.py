"""Thin adapters over the pinned sibling ClawTune implementation.

ClawBox owns artifact validation, freezing, and managed VM policy. Command
normalization, fallback-node construction, target semantics, and conditional
P90 remain ClawTune code and must not drift into a second implementation here.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import ToolObservation


def _load_clawtune() -> tuple[Any, Any, Any, Any, Any]:
    candidates = [
        os.getenv("CLAWTUNE_SIDECAR_SRC"),
        str(Path(__file__).resolve().parents[3] / "ClawTune" / "services" / "sidecar" / "src"),
        "/opt/clawtune/services/sidecar/src",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir() and candidate not in sys.path:
            sys.path.insert(0, candidate)
    try:
        from tool_resource.runtime_kb import (  # type: ignore[import-not-found]
            CompletedCall,
            RuntimeToolResourceKB,
            ToolCallQuery,
        )
        from tool_time.command import (  # type: ignore[import-not-found]
            shell_command_heads as native_heads,
            shell_command_prefix_tokens as native_prefix_tokens,
        )
    except ImportError as exc:  # pragma: no cover - production image gate
        raise RuntimeError("pinned ClawTune package is unavailable") from exc
    return CompletedCall, RuntimeToolResourceKB, ToolCallQuery, native_heads, native_prefix_tokens


def to_epoch(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def shell_command_heads(command: str | None) -> list[str]:
    if not command:
        return []
    return list(_load_clawtune()[3](command))


def shell_command_prefix_tokens(command: str | None) -> list[str]:
    if not command:
        return []
    return list(_load_clawtune()[4](command))


def observation_to_completed_call(observation: ToolObservation, repo: str) -> Any:
    """Adapt one validated ClawBox observation to ClawTune's native record."""
    CompletedCall = _load_clawtune()[0]
    end = observation.end_time
    start = observation.start_time or end
    if end is None or start is None:
        end_epoch = to_epoch(observation.created_at)
        start_epoch = end_epoch - (observation.duration_sec or 0.0)
    else:
        start_epoch = to_epoch(start)
        end_epoch = to_epoch(end)
    cpu_cores = observation.cpu_utilization_avg_cores
    rss_bytes = observation.rss_peak_bytes
    return CompletedCall(
        repo=repo,
        tool_name=observation.tool_name,
        command=observation.command,
        ts_start=round(start_epoch, 6),
        ts_end=round(end_epoch, 6),
        censored=not observation.complete or observation.exit_code != 0,
        peak_cpu_cores=float(cpu_cores) if cpu_cores is not None else None,
        peak_cpu_cores_eligible=cpu_cores is not None,
        peak_memory_mb=(
            float(rss_bytes) / (1024.0 * 1024.0) if rss_bytes is not None else None
        ),
        peak_memory_mb_eligible=rss_bytes is not None,
        ambient_before_mb=0.0 if rss_bytes is not None else None,
    )


def build_clawtune_kb_snapshot(
    observations: list[ToolObservation], repo: str,
) -> dict[str, Any]:
    """Build and validate a snapshot exclusively through native ClawTune APIs."""
    if not observations:
        raise ValueError("ClawTune KB snapshot requires at least one observation")
    _, RuntimeToolResourceKB, ToolCallQuery, _, _ = _load_clawtune()
    calls = [observation_to_completed_call(item, repo) for item in observations]
    kb = RuntimeToolResourceKB.fit_public(calls)
    for call in calls:
        kb.observe_completed_call(call)
    # Strictly later than every completion, so the frozen repo layer contains
    # the recording corpus without weakening ClawTune's causal visibility rule.
    advance_ts = max(call.ts_end for call in calls) + 1e-6
    first = calls[0]
    kb.query(ToolCallQuery(
        repo=repo,
        tool_name=first.tool_name,
        command=first.command,
        ts_start=advance_ts,
        ambient_before_mb=0.0,
    ))
    snapshot = kb.to_json_obj()
    RuntimeToolResourceKB.from_json_obj(snapshot)
    return snapshot
