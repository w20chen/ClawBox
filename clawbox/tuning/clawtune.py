"""Thin adapters over the main-branch sibling ClawTune implementation.

ClawBox owns artifact validation, freezing, and managed VM policy. Command
normalization, fallback-node construction, target semantics, and conditional
P90 remain ClawTune code and must not drift into a second implementation here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
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


def observation_to_completed_call(observation: ToolObservation, repo: str, *, telemetry: dict | None = None) -> Any:
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
    call = CompletedCall(
        repo=repo,
        tool_name=observation.tool_name,
        command=observation.command,
        ts_start=round(start_epoch, 6),
        ts_end=round(end_epoch, 6),
        censored=not observation.complete or observation.status_code in {"timeout", "cancelled", "interrupted"},
        confirmed_timeout=observation.confirmed_timeout or observation.status_code == "timeout",
        timeout_duration_lower_bound_ms=observation.timeout_duration_lower_bound_ms,
        outcome="ok" if observation.exit_code == 0 else "error",
        workload_duration_required=True,
        cpu_time_seconds=observation.cpu_time_sec,
        cpu_avg_cores=observation.cpu_utilization_avg_cores,
        cpu_time_eligible=observation.cpu_time_sec is not None,
        # Average cgroup CPU must never train the fixed-window peak target.
        cpu_peak_cores=None,
        cpu_peak_cores_eligible=False,
        memory_baseline_bytes=observation.memory_baseline_bytes,
        memory_total_peak_bytes=observation.memory_total_peak_bytes,
        memory_extra_peak_bytes=observation.memory_extra_peak_bytes,
        memory_measurement=observation.memory_measurement,
        memory_environment_id=observation.memory_environment_id,
        memory_eligible=observation.memory_eligible,

    )
    pmu = observation.pmu
    if pmu is not None and pmu.execution_id == observation.execution_id:
        values = {f"pmu_{name}": event.raw_count for name, event in pmu.events.items()}
        values.update({f"pmu_{name}": value for name, value in pmu.derived.items()})
        values["pmu_eligible"] = observation.pmu_eligible_for_kb
        call = replace(call, **values)
    if telemetry is not None:
        call = with_execution_labels(call, telemetry)
    return call


def with_execution_labels(call: Any, telemetry: dict) -> Any:
    """Project native execution counters; ClawTune owns missing-target imputation."""
    from clawtune_sidecar.predictors.tool_resource import _retained_workload_duration_seconds
    from tool_resource.sdk import clause_cpu_time_ns

    clauses = telemetry.get("clauses") or []
    counters = [clause_cpu_time_ns(row) for row in clauses]
    profile = telemetry.get("call_resource") or {}
    availability = profile.get("availability") or {}
    peak = profile.get("peak_cpu_cores")
    rss = profile.get("sampled_peak_rss_mb")
    return replace(
        call,
        workload_duration_seconds=_retained_workload_duration_seconds({"calls": [telemetry]}),
        workload_duration_required=True,
        cpu_time_seconds=(sum(value for value in counters if value is not None) / 1e9
                          if any(value is not None for value in counters) else call.cpu_time_seconds),
        cpu_peak_cores=peak,
        cpu_peak_window_ms=500 if peak is not None else None,
        cpu_peak_cores_eligible=availability.get("cpu") == "ok",
        sampled_peak_rss_bytes=rss * 1_000_000 if rss is not None else None,
        sampled_peak_rss_eligible=availability.get("memory") == "ok",
        censored=call.censored or telemetry.get("censored") is True,
        confirmed_timeout=call.confirmed_timeout or telemetry.get("confirmed_timeout") is True,
        timeout_duration_lower_bound_ms=(telemetry.get("timeout_duration_lower_bound_ms")
                                         or call.timeout_duration_lower_bound_ms),
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
        memory_measurement="guest_memtotal_minus_memavailable",
    ))
    snapshot = kb.to_json_obj()
    RuntimeToolResourceKB.from_json_obj(snapshot)
    return snapshot


def predict_native_call_load_models(runtime, query, *, clause=None, lattice=None):
    """Return every native ClawTune load model without selecting across them."""
    from clawbox.clawtune_integration import use_clawtune
    use_clawtune()
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from clawtune_sidecar.prediction_config import load_bucket_edges
    from tool_resource.runtime_kb import ClauseResourceKB
    from tool_time.lattice_kb import LatticeTimeKB
    tool, diagnostics = predict_call_load(
        runtime=runtime, trie=clause if clause is not None else ClauseResourceKB(),
        lattice=lattice if lattice is not None else LatticeTimeKB(), query=query,
        edges=load_bucket_edges((100.0, 500.0, 2000.0, 10000.0)),
    )
    return {
        "tool": tool,
        "trie": diagnostics.backends["trie"],
        "lattice": diagnostics.backends["lattice"],
        "diagnostics": diagnostics,
    }


def predict_native_call_load(runtime, query, *, clause=None, lattice=None):
    """Select LatticeKB for ClawBox scheduling, without cross-model fallback."""
    if lattice is None:
        raise ValueError("ClawBox scheduling requires a LatticeKB snapshot")
    return predict_native_call_load_models(
        runtime, query, clause=clause, lattice=lattice,
    )["lattice"]
