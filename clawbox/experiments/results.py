from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import json
from pathlib import Path
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
    sandbox_task_uid: str | None = None
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


def summary_arm_entry(result: ResultEnvelope) -> dict[str, Any]:
    """Return the compact run-summary entry for one canonical arm result."""
    return {
        "arm": {
            "arm_id": result.arm.arm_id,
            "policy": {"name": result.arm.policy.name},
            "concurrency": result.arm.concurrency,
            "repetition": result.arm.repetition,
            "spec_digest": result.arm.spec_digest,
        },
        "status": result.status.value,
        "failure_category": (
            result.failure_category.value if result.failure_category is not None else None
        ),
        "correctness": result.correctness,
        "result": f"arms/{result.arm.arm_id}.json",
    }


def load_summary_arm_result(run_root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Load a canonical arm result referenced by a compact run summary."""
    reference = entry.get("result")
    if not isinstance(reference, str) or not reference:
        raise ValueError("run summary entry has no canonical arm result reference")
    relative = Path(reference)
    if relative.is_absolute() or relative.parent != Path("arms"):
        raise ValueError(f"invalid arm result reference: {reference}")
    path = run_root / relative
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read arm result {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"arm result is not an object: {path}")
    expected = (entry.get("arm") or {}).get("arm_id")
    actual = (value.get("arm") or {}).get("arm_id")
    if not expected or actual != expected:
        raise ValueError(f"arm result identity mismatch: {path}")
    return value


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
