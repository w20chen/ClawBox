from __future__ import annotations

from pydantic import BaseModel, Field

from clawbox.experiments import ExperimentSpec


class CreateRunRequest(BaseModel):
    projectId: str = Field(default="default", min_length=1, max_length=128)
    experimentSpec: ExperimentSpec
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
    phase: str
    desiredState: str | None = None
    currentAttemptId: str | None = None
    attemptCounter: int
    experimentId: str
    specDigest: str
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
