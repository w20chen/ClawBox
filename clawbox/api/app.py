"""Managed API server (M1, ADR-001/002).

FastAPI app factory so tests inject a session factory + template registry.
Restart-safe: every handler reads/writes through the repository (no in-process
memory is authoritative). Auth: X-Clawbox-Token (service token) plus
X-Tenant-Id for tenant scoping on every resource.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from clawbox.api.schemas import (
    AttemptResponse,
    CreateRunRequest,
    CreateRunResponse,
    RunEventResponse,
    RunResponse,
)
from clawbox.api.templates import TemplateError, TemplateRegistry, default_registry
from clawbox.managed.db import managed_session_factory
from clawbox.managed.models import RunIntent, idempotency_digest
from clawbox.managed.repo import (
    RunConflict,
    create_run,
    get_attempt,
    get_run,
    load_run_events,
    new_attempt,
    request_cancel,
)


def create_app(
    *,
    session_factory=None,
    registry: TemplateRegistry | None = None,
    service_token: str | None = None,
) -> FastAPI:
    factory = session_factory or managed_session_factory()
    templates = registry or default_registry()
    service_token = service_token or os.getenv("CLAWBOX_SERVICE_TOKEN", "development-only-token")

    app = FastAPI(title="ClawBox Managed API", version="0.2.0")

    def check_auth(
        x_clawbox_token: Annotated[str, Header()] = "",
    ) -> None:
        if x_clawbox_token != service_token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid service token")

    def tenant(x_tenant_id: Annotated[str, Header()] = "") -> str:
        if not x_tenant_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Tenant-Id header is required")
        return x_tenant_id

    def db() -> Any:
        session: Session = factory()
        try:
            yield session
            session.commit()
        except HTTPException:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/runs", response_model=CreateRunResponse, status_code=status.HTTP_201_CREATED)
    def create_run_endpoint(
        body: CreateRunRequest,
        response: Response,
        _: None = Depends(check_auth),
        tenant_id: str = Depends(tenant),
        session: Session = Depends(db),
    ) -> CreateRunResponse:
        try:
            policy = templates.resolve(body.templateRef, body.templateRevision)
            templates.validate_deadline(policy, body.deadlineSeconds)
        except TemplateError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

        # request_digest is computed from the server-canonicalized body so a
        # replay returns the original Run and a different body is a 409.
        digest_payload = body.model_dump()
        intent = RunIntent(
            tenant_id=tenant_id,
            project_id=body.projectId,
            template_ref=body.templateRef,
            template_revision=body.templateRevision,
            input_ref=body.inputRef,
            input_sha256=body.inputSha256,
            deadline_seconds=body.deadlineSeconds,
            idempotency_key=body.idempotencyKey,
            request_digest=idempotency_digest(digest_payload),
            problem_statement=body.problemStatement,
        )
        try:
            run, created = create_run(session, intent)
        except RunConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return CreateRunResponse(
            runId=run.run_id,
            phase=run.phase.value,
            idempotencyReplay=not created,
            currentAttemptId=run.current_attempt_id,
        )

    @app.get("/v1/runs/{run_id}", response_model=RunResponse)
    def get_run_endpoint(
        run_id: str,
        _: None = Depends(check_auth),
        tenant_id: str = Depends(tenant),
        session: Session = Depends(db),
    ) -> RunResponse:
        run = get_run(session, tenant_id, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
        return RunResponse(
            runId=run.run_id,
            tenantId=run.tenant_id,
            projectId=run.project_id,
            templateRef=run.template_ref,
            templateRevision=run.template_revision,
            inputRef=run.input_ref,
            inputSha256=run.input_sha256,
            deadlineSeconds=run.deadline_seconds,
            phase=run.phase.value,
            desiredState=run.desired_state,
            currentAttemptId=run.current_attempt_id,
            attemptCounter=run.attempt_counter,
            createdAt=run.created_at,
            updatedAt=run.updated_at,
            committedAt=run.committed_at,
            finalReason=run.final_reason,
        )

    @app.post("/v1/runs/{run_id}/cancel", response_model=RunResponse)
    def cancel_run_endpoint(
        run_id: str,
        _: None = Depends(check_auth),
        tenant_id: str = Depends(tenant),
        session: Session = Depends(db),
    ) -> RunResponse:
        run = get_run(session, tenant_id, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
        request_cancel(session, run)
        return get_run_endpoint(run_id, None, tenant_id, session)

    @app.post("/v1/runs/{run_id}/retry", response_model=AttemptResponse)
    def retry_run_endpoint(
        run_id: str,
        _: None = Depends(check_auth),
        tenant_id: str = Depends(tenant),
        session: Session = Depends(db),
    ) -> AttemptResponse:
        run = get_run(session, tenant_id, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
        try:
            attempt = new_attempt(session, run)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return _attempt_response(attempt)

    @app.get("/v1/runs/{run_id}/attempts", response_model=list[AttemptResponse])
    def list_attempts_endpoint(
        run_id: str,
        _: None = Depends(check_auth),
        tenant_id: str = Depends(tenant),
        session: Session = Depends(db),
    ) -> list[AttemptResponse]:
        from sqlalchemy import select

        from clawbox.managed.db import AttemptRow

        run = get_run(session, tenant_id, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
        rows = session.scalars(
            select(AttemptRow).where(AttemptRow.run_id == run_id).order_by(AttemptRow.attempt_number)
        ).all()
        return [_attempt_response(get_attempt(session, row.attempt_id)) for row in rows]

    @app.get("/v1/runs/{run_id}/events", response_model=list[RunEventResponse])
    def list_events_endpoint(
        run_id: str,
        _: None = Depends(check_auth),
        tenant_id: str = Depends(tenant),
        session: Session = Depends(db),
    ) -> list[RunEventResponse]:
        if get_run(session, tenant_id, run_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
        return [
            RunEventResponse(
                sequence=e.sequence,
                runId=e.run_id,
                attemptId=e.attempt_id,
                eventType=e.event_type,
                phase=e.phase,
                reason=e.reason,
                message=e.message,
                observedGeneration=e.observed_generation,
                lastTransitionTime=e.last_transition_time,
            )
            for e in load_run_events(session, run_id)
        ]

    return app


def _attempt_response(attempt) -> AttemptResponse:
    return AttemptResponse(
        attemptId=attempt.attempt_id,
        runId=attempt.run_id,
        attemptNumber=attempt.attempt_number,
        phase=attempt.phase.value,
        platformOutcome=attempt.platform_outcome.value,
        agentOutcome=attempt.agent_outcome.value,
        artifactOutcome=attempt.artifact_outcome.value,
        evaluationOutcome=attempt.evaluation_outcome.value,
        startedAt=attempt.started_at,
        finishedAt=attempt.finished_at,
        resultManifestRef=attempt.result_manifest_ref,
    )


# Module-level default app for uvicorn/entry point.
app = create_app()
