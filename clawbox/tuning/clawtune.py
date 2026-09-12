"""Thin adapters over the main-branch sibling ClawTune implementation.

ClawBox owns artifact validation, freezing, and managed VM policy. Command
normalization, fallback-node construction, target semantics, and conditional
P90 remain ClawTune code and must not drift into a second implementation here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .schema import ToolObservation


def _load_clawtune() -> tuple[Any, Any, Any, Any, Any]:
    from clawbox.clawtune_integration import use_clawtune
    use_clawtune()
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
        raise RuntimeError("ClawTune main package is unavailable") from exc
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
    pmu = observation.pmu
    pmu_eligible = bool(
        observation.pmu_eligible_for_kb and pmu is not None
        and pmu.execution_id == observation.execution_id
        and pmu.coverage.status == "reliable" and pmu.coverage.eligible_for_kb
    )
    return CompletedCall(
        repo=repo,
        tool_name=observation.tool_name,
        command=observation.command,
        ts_start=round(start_epoch, 6),
        ts_end=round(end_epoch, 6),
        censored=not observation.complete or observation.exit_code != 0,
        cpu_time_seconds=observation.cpu_time_sec,
        cpu_time_eligible=observation.cpu_time_sec is not None and observation.complete,
        # Average cgroup CPU must never train the fixed-window peak target.
        peak_cpu_cores=None,
        peak_cpu_cores_eligible=False,
        peak_memory_mb=(
            float(rss_bytes) / (1024.0 * 1024.0) if rss_bytes is not None else None
        ),
        peak_memory_mb_eligible=rss_bytes is not None,
        ambient_before_mb=0.0 if rss_bytes is not None else None,
        pmu_ipc=pmu.derived["ipc"] if pmu_eligible else None,
        pmu_llc_mpki=pmu.derived["llc_mpki"] if pmu_eligible else None,
        pmu_llc_miss_rate=pmu.derived["llc_miss_rate"] if pmu_eligible else None,
        pmu_eligible=pmu_eligible,
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


def predict_native_call_load(runtime, query, *, clause=None, lattice=None):
    """Use ClawTune's canonical five-target adapter and its evidence semantics."""
    from clawbox.clawtune_integration import use_clawtune
    use_clawtune()
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from clawtune_sidecar.prediction_config import load_bucket_edges
    from tool_resource.runtime_kb import ClauseResourceKB
    from tool_time.lattice_kb import LatticeTimeKB
    return predict_call_load(
        runtime=runtime, trie=clause if clause is not None else ClauseResourceKB(),
        lattice=lattice if lattice is not None else LatticeTimeKB(), query=query,
        edges=load_bucket_edges((100.0, 500.0, 2000.0, 10000.0)),
    )[0]
