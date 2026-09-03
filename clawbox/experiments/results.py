from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .spec import ExperimentArm


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
    CAPACITY = "capacity"
    OOM = "oom"
    TIMEOUT = "timeout"
    VALIDATION = "validation"
    INFRASTRUCTURE = "infrastructure"


class ResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 2
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    arm: ExperimentArm
    provenance: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus
    failure_category: FailureCategory | None = None
    started_at: datetime
    completed_at: datetime
    correctness: dict[str, Any] = Field(default_factory=dict)
    performance: dict[str, Any] = Field(default_factory=dict)
    memory: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def status_and_failure_category_agree(self) -> "ResultEnvelope":
        if self.status is RunStatus.SUCCEEDED and self.failure_category is not None:
            raise ValueError("successful results cannot have a failure category")
        if self.status not in {RunStatus.SUCCEEDED, RunStatus.CANCELLED} and self.failure_category is None:
            raise ValueError("failed results require a failure category")
        return self


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def failure_category_for(status: RunStatus, detail: str = "") -> FailureCategory | None:
    if status in {RunStatus.SUCCEEDED, RunStatus.CANCELLED}:
        return None
    if status is RunStatus.TIMED_OUT:
        return FailureCategory.TIMEOUT
    text = detail.lower()
    for marker, category in (
        ("capacity", FailureCategory.CAPACITY), ("out of memory", FailureCategory.OOM),
        ("oom", FailureCategory.OOM), ("validation", FailureCategory.VALIDATION),
        ("admission", FailureCategory.ADMISSION), ("sandbox", FailureCategory.SANDBOX),
        ("tool", FailureCategory.TOOL), ("inference", FailureCategory.INFERENCE),
        ("agent", FailureCategory.AGENT), ("workload", FailureCategory.WORKLOAD),
    ):
        if marker in text:
            return category
    return FailureCategory.INFRASTRUCTURE
