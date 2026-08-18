"""Exact execution_id join between ClawTune spans and bridge records.

Pre-ADR-007 data joined these two sources by a time-window heuristic, which
made the observation dataset untrustworthy.  With the SSH command envelope the
ClawTune span and the tool-bridge record share the same ``execution_id``, so the
join is exact: no time window, deterministic, and fully testable (join rate
must be 100% on well-formed input).

The joiner is deliberately bidirectional-safe:

* A span without a bridge record (e.g. a tool that never reached the bridge)
  is reported as ``unmatched_spans``.
* A bridge record without a span is reported as ``unmatched_bridges``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .schema import BridgeRecord, ObservationSource, ToolObservation, span_end_to_observation


@dataclass(frozen=True)
class JoinResult:
    joined: tuple[ToolObservation, ...] = ()
    unmatched_spans: tuple[ToolObservation, ...] = ()
    unmatched_bridges: tuple[BridgeRecord, ...] = ()

    @property
    def join_rate(self) -> float:
        """Fraction of spans that found an exact bridge match."""
        total = len(self.joined) + len(self.unmatched_spans)
        if total == 0:
            return 1.0
        return len(self.joined) / total

    @property
    def span_count(self) -> int:
        return len(self.joined) + len(self.unmatched_spans)


def _bridge_to_observation(bridge: BridgeRecord) -> ToolObservation:
    return ToolObservation(
        execution_id=bridge.execution_id,
        source=ObservationSource.TOOL_BRIDGE,
        complete=True,
        exit_code=bridge.exit_code,
        duration_sec=bridge.duration_ms / 1000.0,
        collection_quality="valid" if not bridge.timed_out else "degraded",
        status_code="timeout" if bridge.timed_out else ("ok" if bridge.exit_code == 0 else "error"),
        stdout_bytes=bridge.stdout_bytes,
        stderr_bytes=bridge.stderr_bytes,
        output_truncated=bridge.output_truncated,
        trusted=not bridge.timed_out and bridge.exit_code == 0,
    )


def join_trace_and_bridge(
    span_records: list[dict[str, Any]],
    bridge_records: list[BridgeRecord],
) -> JoinResult:
    """Join span_end records with bridge records on execution_id (exact).

    Parameters
    ----------
    span_records:
        Raw ClawTune v6 JSONL records (only ``span_end`` tool records with an
        execution_id are considered; the rest are ignored).
    bridge_records:
        Parsed tool-bridge execution records.
    """
    spans: list[ToolObservation] = []
    for record in span_records:
        observation = span_end_to_observation(record)
        if observation is not None:
            spans.append(observation)
    by_id: dict[str, list[BridgeRecord]] = {}
    for bridge in bridge_records:
        by_id.setdefault(bridge.execution_id, []).append(bridge)
    joined: list[ToolObservation] = []
    unmatched_spans: list[ToolObservation] = []
    used_bridge_ids: set[str] = set()
    for span in spans:
        candidates = by_id.get(span.execution_id, [])
        bridge = candidates[0] if candidates else None
        if bridge is None:
            unmatched_spans.append(span)
            continue
        # Merge span-side resource/latency fields with bridge-side exit/output
        # fields.  Span values win for resources; bridge exit_code wins (it is
        # the authoritative process exit status from the actual shell).
        merged = span.model_copy(update={})
        merged.exit_code = bridge.exit_code
        if merged.duration_sec is None:
            merged.duration_sec = bridge.duration_ms / 1000.0
        if merged.stdout_bytes is None:
            merged.stdout_bytes = bridge.stdout_bytes
        if merged.stderr_bytes is None:
            merged.stderr_bytes = bridge.stderr_bytes
        merged.output_truncated = bridge.output_truncated or merged.output_truncated
        merged.complete = True
        if bridge.timed_out:
            merged.status_code = "timeout"
        if bridge.exit_code != 0 and merged.status_code == "ok":
            merged.status_code = "error"
        merged.trusted = merged.collection_quality == "valid" and merged.complete and merged.exit_code == 0
        joined.append(merged)
        used_bridge_ids.add(bridge.execution_id)
    unmatched_bridges = [
        bridge for bridge in bridge_records if bridge.execution_id not in used_bridge_ids
    ]
    return JoinResult(
        joined=tuple(joined),
        unmatched_spans=tuple(unmatched_spans),
        unmatched_bridges=tuple(unmatched_bridges),
    )
