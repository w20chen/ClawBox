from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionIntent(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    execution_id: str = Field(min_length=1, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    command: str = Field(min_length=1)
    argv: list[str] = Field(default_factory=list)
    repo_fingerprint: str = Field(default="unknown", max_length=256)
    tool_image: str = Field(default="clawbox-tool:latest", max_length=512)
    workspace_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime = Field(default_factory=utcnow)


class ResourcePrediction(StrictModel):
    execution_id: str
    cpu_p90: float = Field(gt=0)
    memory_p90: int = Field(gt=0)
    duration_p50: float = Field(ge=0)
    duration_p90: float = Field(ge=0)
    time_bucket: str
    match_level: str
    sample_count: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    kb_generation: int = Field(ge=0)


class ResourceRequest(StrictModel):
    """Allocator-safe request: deliberately has no command/repo/output fields."""

    tenant_id: str
    execution_id: str
    cpu_count: int = Field(gt=0)
    memory_bytes: int = Field(gt=0)
    preferred_numa: int | None = Field(default=None, ge=0)
    expected_duration: float = Field(ge=0)
    priority: int = 0


class LeaseState(StrEnum):
    ACTIVE = "ACTIVE"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    RESOURCE_RECLAIMABLE = "RESOURCE_RECLAIMABLE"
    RELEASED = "RELEASED"


class ResourceLease(StrictModel):
    lease_id: str
    tenant_id: str
    execution_id: str
    cpu_count: int
    memory_bytes: int
    numa_hint: int | None
    allocator_epoch: str
    fencing_token: int
    created_at: datetime
    expires_at: datetime
    state: LeaseState = LeaseState.ACTIVE


class RenewLease(StrictModel):
    ttl_seconds: int = Field(default=300, ge=5, le=86400)
    fencing_token: int = Field(gt=0)


class ReleaseLease(StrictModel):
    fencing_token: int = Field(gt=0)
    workload_stopped: bool = False


class ToolSpec(StrictModel):
    tenant_id: str
    execution_id: str
    workspace_id: str
    cpu_count: int = Field(gt=0)
    memory_bytes: int = Field(gt=0)
    numa_hint: int | None = None
    image: str
    mode: Literal["session", "benchmark"] = "session"


class ToolInstance(StrictModel):
    tool_pod_uid: str
    tenant_id: str
    workspace_id: str
    endpoint: str
    backend: Literal["docker", "subprocess"]
    state: str


class ExecutionGrant(StrictModel):
    tenant_id: str
    execution_id: str
    tool_pod_uid: str
    workspace_id: str
    command_digest: str
    lease_id: str
    cpu_count: int
    numa_hint: int | None
    allocator_epoch: str
    fencing_token: int
    expires_at: datetime
    nonce: str
    signature: str


class ExecuteRequest(StrictModel):
    grant: ExecutionGrant
    command: str


class Observation(StrictModel):
    tenant_id: str
    execution_id: str
    observation_type: str = "tool-execution"
    version: int = 1
    start_time: datetime
    end_time: datetime
    exit_code: int
    cpu: dict[str, Any] = Field(default_factory=dict)
    memory: dict[str, Any] = Field(default_factory=dict)
    disk: dict[str, Any] = Field(default_factory=dict)
    network: dict[str, Any] = Field(default_factory=dict)
    process_events: list[dict[str, Any]] = Field(default_factory=list)
    collection_quality: Literal["valid", "degraded", "invalid"]
    collector_version: str
    tool_image_digest: str
    complete: bool
    cgroup: str | None = None

    @model_validator(mode="after")
    def valid_interval(self) -> "Observation":
        if self.end_time < self.start_time:
            raise ValueError("end_time precedes start_time")
        return self


class ExecutionResult(StrictModel):
    execution_id: str
    tenant_id: str
    prediction: ResourcePrediction
    lease: ResourceLease
    tool: ToolInstance
    observation: Observation
    kb_generation_before: int
    kb_generation_after: int
    lease_final_state: LeaseState
    stdout: str = ""
    stderr: str = ""

