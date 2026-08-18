"""Managed API pydantic schemas (M1)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateRunRequest(BaseModel):
    projectId: str = Field(min_length=1, max_length=128)
    templateRef: str = Field(min_length=1, max_length=256)
    templateRevision: int = Field(ge=1)
    inputRef: str = Field(min_length=1, max_length=512)
    inputSha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    deadlineSeconds: int = Field(ge=60, le=86400)
    idempotencyKey: str = Field(min_length=1, max_length=512)


class CreateRunResponse(BaseModel):
    runId: str
    phase: str
    idempotencyReplay: bool
    currentAttemptId: str | None = None


class RunResponse(BaseModel):
    runId: str
    tenantId: str
    projectId: str
    templateRef: str
    templateRevision: int
    inputRef: str
    inputSha256: str
    deadlineSeconds: int
    phase: str
    desiredState: str | None = None
    currentAttemptId: str | None = None
    attemptCounter: int
    createdAt: str | None = None
    updatedAt: str | None = None
    committedAt: str | None = None
    finalReason: str | None = None


class AttemptResponse(BaseModel):
    attemptId: str
    runId: str
    attemptNumber: int
    phase: str
    platformOutcome: str
    agentOutcome: str
    artifactOutcome: str
    evaluationOutcome: str
    startedAt: str | None = None
    finishedAt: str | None = None
    resultManifestRef: str | None = None


class RunEventResponse(BaseModel):
    sequence: int
    runId: str
    attemptId: str | None = None
    eventType: str
    phase: str
    reason: str | None = None
    message: str | None = None
    observedGeneration: int
    lastTransitionTime: str | None = None


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
