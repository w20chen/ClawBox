"""Stable outer result metadata; existing runner artifacts remain authoritative detail."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .capabilities import WorkflowClassification
from .spec import ResolvedWorkflow


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class FailureCategory(StrEnum):
    WORKLOAD = "workload"
    AGENT = "agent"
    INFERENCE = "inference"
    TOOL = "tool"
    SANDBOX = "sandbox"
    ADMISSION = "admission"
    OOM = "oom"
    TIMEOUT = "timeout"
    VALIDATION = "validation"
    INFRASTRUCTURE = "infrastructure"


class ResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    baseline: str
    classification: WorkflowClassification
    resolved_workflow: ResolvedWorkflow
    provenance: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus
    failure_category: FailureCategory | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    backend_details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def status_and_failure_category_agree(self) -> "ResultEnvelope":
        terminal_without_failure = {RunStatus.SUCCEEDED, RunStatus.CANCELLED}
        if self.status in terminal_without_failure and self.failure_category is not None:
            raise ValueError(f"status={self.status} must not have a failure_category")
        if self.status not in terminal_without_failure and self.failure_category is None:
            raise ValueError(f"status={self.status} requires a failure_category")
        return self


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def failure_category_for(status: RunStatus, detail: str = "") -> FailureCategory | None:
    if status is RunStatus.SUCCEEDED or status is RunStatus.CANCELLED:
        return None
    if status is RunStatus.TIMED_OUT:
        return FailureCategory.TIMEOUT
    text = detail.lower()
    for marker, category in (
        ("out of memory", FailureCategory.OOM), ("oom", FailureCategory.OOM),
        ("validation", FailureCategory.VALIDATION), ("admission", FailureCategory.ADMISSION),
        ("sandbox", FailureCategory.SANDBOX), ("firecracker", FailureCategory.SANDBOX),
        ("tool", FailureCategory.TOOL), ("inference", FailureCategory.INFERENCE),
        ("agent", FailureCategory.AGENT), ("workload", FailureCategory.WORKLOAD),
    ):
        if marker in text:
            return category
    return FailureCategory.INFRASTRUCTURE
