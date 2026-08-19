"""Normalized tool-execution observation schema (ADR-008 / ADR-007).

Two raw sources feed the pipeline:

1. **ClawTune v6 span JSONL** (runtime side, written by the plugin): one record
   per line, ``record_type`` in {``trace_metadata``, ``span_start``,
   ``span_end``}.  The ``span_end`` record carries the tool resources
   (cpu/rss/coverage) and ``execution.execution_id``.
2. **Tool-bridge execution JSONL** (tool VM side, ``tool-bridge.jsonl``): one
   record per shell command with exit code, wall duration, stdout/stderr byte
   counts and the same ``execution_id`` (carried through the SSH envelope).

``ToolObservation`` is the joined, normalized unit that flows into the dataset
and the KB.  It is deliberately immutable (frozen) and extra-forbidden.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectionQuality(StrEnum):
    VALID = "valid"
    DEGRADED = "degraded"
    INVALID = "invalid"


class ObservationSource(StrEnum):
    CLAWTUNE_SPAN = "clawtune_span"
    TOOL_BRIDGE = "tool_bridge"


#: Trace schema version the ClawTune plugin emits (trace v6).
CLAWTUNE_TRACE_SCHEMA_VERSION = 6

#: Schema version of the normalized ToolObservation.
OBSERVATION_SCHEMA_VERSION = 1

#: Allowed duration buckets, aligned with the legacy KB bucket helper.
DURATION_BUCKETS = ("short", "medium", "long")


def duration_bucket(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if seconds <= 1.0:
        return "short"
    if seconds <= 30.0:
        return "medium"
    return "long"


class BridgeRecord(StrictModel):
    """One line of the tool-bridge execution JSONL."""

    timestamp: str
    cell_id: str | None = None
    task_id: str | None = None
    execution_id: str
    execution_source: Literal["bridge-local", "runtime-envelope"] | None = None
    command_sha256: str | None = None
    command_bytes: int | None = None
    duration_ms: int
    exit_code: int
    timed_out: bool = False
    stdout_bytes: int | None = None
    stderr_bytes: int | None = None
    output_truncated: bool = False
    user_cpu_ms: int | None = None
    system_cpu_ms: int | None = None
    max_rss_kib: int | None = None


class CgroupResource(StrictModel):
    """Per-execution cgroup v2 + eBPF resource artifact (ClawTune-compatible).

    Mirrors ClawTune's ``CgroupResourceResult``
    (``services/sidecar/src/clawtune_sidecar/telemetry/cgroup_resource.py``,
    schema ``cgroup_resource_v1``) so a Tool-VM collector that reuses ClawTune's
    ``monitoring`` package produces artifacts we can parse verbatim.  ``source``
    distinguishes the measurement path:

    * ``cgroup-v2``    — cpu/mem/disk from cgroup v2 counters, network from the
                         per-process BCC (eBPF) tracker.
    * ``process-tree`` — psutil process-tree sampling (eBPF unavailable).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ``schema_name`` avoids shadowing pydantic's ``BaseModel.schema``; the
    # JSON key stays ``schema`` (the ClawTune artifact field name).
    schema_name: str = Field(
        default="cgroup_resource_v1",
        validation_alias="schema",
        serialization_alias="schema",
    )
    execution_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str = ""
    source: str = "cgroup-v2"
    monitor_source: str | None = None
    attribution_source: str | None = None
    ts_start: float | None = None
    ts_end: float | None = None
    duration_ms: int | None = None
    cpu_time_s: float | None = None
    cpu_utilization_avg_cores: float | None = None
    memory_rss_before_bytes: int | None = None
    memory_rss_after_bytes: int | None = None
    memory_rss_peak_bytes: int | None = None
    disk_read_bytes_delta: int | None = None
    disk_write_bytes_delta: int | None = None
    network_rx_bytes_delta: int | None = None
    network_tx_bytes_delta: int | None = None
    sampling_interval_ms: int | None = None
    sampling_point_count: int | None = None
    sampling_quality: str | None = None


def cgroup_artifact_to_resource(
    data: dict[str, Any],
) -> CgroupResource | None:
    """Parse one ClawTune ``cgroup-resource-<execution_id>.json`` artifact.

    Returns ``None`` when the payload is not a cgroup_resource_v1 record or
    lacks an execution_id (so no dangling reference is attached).
    """
    if not isinstance(data, dict):
        return None
    if data.get("schema") not in (None, "cgroup_resource_v1"):
        return None
    if not data.get("execution_id"):
        return None
    try:
        return CgroupResource.model_validate(data)
    except (ValueError, TypeError):
        return None


class ToolObservation(StrictModel):
    """Normalized, joined, validated unit fed to the dataset and KB.

    ``trusted`` is set by the validator: complete + valid quality + exit 0.
    Only trusted observations train the active KB.
    """

    schema_version: int = OBSERVATION_SCHEMA_VERSION
    execution_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(default="exec", max_length=128)
    command: str | None = Field(default=None, max_length=4096)
    command_digest: str | None = Field(default=None, max_length=64)
    repo_fingerprint: str | None = Field(default=None, max_length=256)
    run_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    sequence_no: int = 0
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_sec: float | None = Field(default=None, ge=0)
    status_code: str | None = Field(default=None, max_length=32)
    exit_code: int | None = None
    complete: bool = False
    collection_quality: CollectionQuality = CollectionQuality.INVALID
    coverage_ratio: float | None = Field(default=None, ge=0, le=1)
    coverage_reason: str | None = Field(default=None, max_length=64)
    cpu_time_sec: float | None = Field(default=None, ge=0)
    cpu_utilization_avg_cores: float | None = Field(default=None, ge=0)
    rss_peak_bytes: int | None = Field(default=None, ge=0)
    memory_rss_bytes_after: int | None = Field(default=None, ge=0)
    stdout_bytes: int | None = Field(default=None, ge=0)
    stderr_bytes: int | None = Field(default=None, ge=0)
    output_truncated: bool = False
    source: ObservationSource = ObservationSource.CLAWTUNE_SPAN
    # Independent cgroup v2 + eBPF resource artifact, when the Tool-VM
    # collector produced one for this execution (ClawTune cgroup_resource_v1).
    cgroup: CgroupResource | None = None
    trusted: bool = False
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def latency_bucket(self) -> str | None:
        return duration_bucket(self.duration_sec)

    @model_validator(mode="after")
    def _valid_interval(self) -> "ToolObservation":
        if self.start_time is not None and self.end_time is not None:
            if self.end_time < self.start_time:
                raise ValueError("end_time precedes start_time")
        if self.collection_quality == CollectionQuality.VALID and self.duration_sec is None:
            raise ValueError("valid observation requires duration_sec")
        return self


# ── Span-end extraction helpers ─────────────────────────────────────────

def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def span_end_to_observation(record: dict[str, Any]) -> ToolObservation | None:
    """Build a normalized observation from a ClawTune v6 ``span_end`` record.

    Returns ``None`` when the record is not a tool span or lacks an
    execution_id (unjoinable in hook-only mode before the envelope fix).
    """
    if record.get("record_type") != "span_end":
        return None
    if record.get("schema_version") != CLAWTUNE_TRACE_SCHEMA_VERSION:
        return None
    if record.get("kind") != "tool":
        return None
    execution = record.get("execution") or {}
    execution_id = execution.get("execution_id")
    if not execution_id:
        return None
    resources = record.get("resources") or {}
    output = record.get("output") or {}
    status = record.get("status") or {}
    try:
        end_time = datetime.fromtimestamp(int(record["wall_time_ns"]) / 1e9, tz=timezone.utc)
    except (KeyError, TypeError, ValueError):
        end_time = None
    start_time = None
    try:
        start_time = datetime.fromtimestamp(
            int(resources.get("monitor_start_wall_time_ns") or record["wall_time_ns"]) / 1e9,
            tz=timezone.utc,
        )
    except (KeyError, TypeError, ValueError):
        start_time = end_time
    duration_sec = _as_float(record.get("duration_sec")) or _as_float(resources.get("action_duration_ns"))
    if duration_sec is None:
        duration_sec = _as_float(record.get("duration_ns"))
        if duration_sec is not None:
            duration_sec = duration_sec / 1e9
    coverage_ratio = _as_float(resources.get("coverage_ratio"))
    status_code = status.get("code")
    exit_code = output.get("exit_code")
    exit_code = _as_int(exit_code) if exit_code is not None else None
    if exit_code is None:
        exit_code = 0 if status_code == "ok" else 1
    quality: CollectionQuality
    if status_code == "ok" and coverage_ratio is not None and coverage_ratio >= 0.9:
        quality = CollectionQuality.VALID
    elif status_code in ("ok", "error", "timeout") and coverage_ratio is not None and coverage_ratio >= 0.5:
        quality = CollectionQuality.DEGRADED
    else:
        quality = CollectionQuality.INVALID
    complete = status_code in ("ok", "error", "timeout", "cancelled")
    requested_command = execution.get("requested_command") or execution.get("payload_command")
    command_digest = execution.get("command_digest")
    cpu_time_sec = _as_float(resources.get("cpu_time_s"))
    cpu_cores = _as_float(resources.get("cpu_utilization_avg_cores"))
    rss_peak = _as_int(resources.get("rss_peak_bytes"))
    rss_after = _as_int(resources.get("memory_rss_bytes_after"))
    try:
        return ToolObservation(
            execution_id=execution_id,
            tool_name=record.get("name") or "exec",
            command=requested_command,
            command_digest=command_digest,
            repo_fingerprint=record.get("repo"),
            run_id=record.get("run_id"),
            session_id=record.get("session_id"),
            sequence_no=int(record.get("sequence_no") or 0),
            start_time=start_time,
            end_time=end_time,
            duration_sec=duration_sec,
            status_code=status_code,
            exit_code=exit_code,
            complete=complete,
            collection_quality=quality,
            coverage_ratio=coverage_ratio,
            coverage_reason=resources.get("coverage_reason"),
            cpu_time_sec=cpu_time_sec,
            cpu_utilization_avg_cores=cpu_cores,
            rss_peak_bytes=rss_peak,
            memory_rss_bytes_after=rss_after,
            source=ObservationSource.CLAWTUNE_SPAN,
        )
    except (ValueError, TypeError):
        return None


def bridge_record_to_observation(record: dict[str, Any]) -> BridgeRecord:
    """Parse one tool-bridge JSONL line into a validated BridgeRecord."""
    return BridgeRecord.model_validate(record)
