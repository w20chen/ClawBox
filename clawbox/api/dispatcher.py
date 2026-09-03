"""Managed Dispatcher (ADR-001, M1-5).

The Dispatcher is the ONLY component that creates SandboxTask CRs. It consumes
the outbox (same transaction as the API writes) and converges:

  run.accepted       -> create the first Attempt (idempotent)
  attempt.created    -> build + apply the v1alpha2 SandboxTask CR, Run -> Queued
  run.cancel_requested -> one-way desiredState=Cancelled on the live CR

Restart-safe: state is read from the repository/outbox, never from memory; the
CR apply is name-idempotent (crash between CR creation and outbox completion is
recovered by re-checking the CR). The CR backend is a protocol so tests inject
a fake.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from clawbox.cell.sandboxtask_v1alpha2 import (
    DESIRED_CANCELLED,
    DESIRED_RUNNING,
    build_sandboxtask_v1alpha2,
    cancel_patch,
)
from clawbox.managed.db import OutboxRow, utcnow
from clawbox.managed.models import (
    AgentOutcome,
    ArtifactOutcome,
    AttemptPhase,
    PlatformOutcome,
    Run,
    RunPhase,
)
from clawbox.managed.repo import (
    complete_outbox,
    get_attempt,
    get_run,
    new_attempt,
    transition_attempt,
    transition_run,
)

logger = logging.getLogger(__name__)

# Cell controller namespace for SandboxTask CRs (matches CLAWBOX_CELL_NAMESPACE).
DEFAULT_CELL_NAMESPACE = "clawbox-benchmarks"


def _dns_label(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return (normalized or "unknown")[:63].rstrip("-")


class CRBackend(Protocol):
    """Create/cancel SandboxTask CRs; must be name-idempotent."""

    def apply_sandboxtask(self, manifest: dict[str, Any]) -> bool: ...

    def cancel_sandboxtask(self, name: str, namespace: str) -> None: ...

    def list_sandboxtasks(self, namespace: str) -> list[dict[str, Any]]: ...


class KubernetesCRBackend:
    """Real backend over the Kubernetes dynamic client.

    `version` is configurable so the dispatcher can keep writing v1alpha1 while
    the cluster is still on the v1alpha1 CRD, then flip to v1alpha2 once the
    conversion webhook and controller flip are live (ADR-003). Note: on a
    v1alpha1 CRD the runRef/desiredState fields are pruned server-side.
    """

    def __init__(
        self, custom_api=None, namespace: str = DEFAULT_CELL_NAMESPACE,
        version: str = "v1alpha2",
    ):
        self.namespace = namespace
        self.version = version
        if custom_api is None:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            custom_api = client.CustomObjectsApi()
        self.custom = custom_api
        from clawbox.cell.controller import GROUP, PLURAL

        self.group = GROUP
        self.plural = PLURAL

    def apply_sandboxtask(self, manifest: dict[str, Any]) -> bool:
        # Align apiVersion with the cluster's served CRD version; the builder
        # emits the v1alpha2 shape, and a v1alpha1 CRD prunes the extra fields.
        manifest = {**manifest, "apiVersion": f"{self.group}/{self.version}"}
        name = manifest["metadata"]["name"]
        namespace = manifest["metadata"].get("namespace", self.namespace)
        try:
            existing = self.custom.get_namespaced_custom_object(
                self.group, self.version, namespace, self.plural, name,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                self.custom.create_namespaced_custom_object(
                    self.group, self.version, namespace, self.plural, manifest,
                )
                return True
            raise
        # Idempotent: if the CR already exists with the same attempt identity,
        # the apply is a no-op (the CR spec is immutable anyway).
        spec = existing.get("spec", {})
        ref = spec.get("runRef", {})
        if ref.get("attemptID") != manifest["spec"]["runRef"]["attemptID"]:
            raise RuntimeError(
                f"SandboxTask {namespace}/{name} already exists for attempt "
                f"{ref.get('attemptID')}, refusing to adopt a different attempt"
            )
        return False

    def cancel_sandboxtask(self, name: str, namespace: str | None = None) -> None:
        ns = namespace or self.namespace
        try:
            self.custom.patch_namespaced_custom_object(
                self.group, self.version, ns, self.plural, name, cancel_patch(),
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                logger.warning("cancel: SandboxTask %s/%s already gone", ns, name)
                return
            raise

    def list_sandboxtasks(self, namespace: str | None = None) -> list[dict[str, Any]]:
        response = self.custom.list_namespaced_custom_object(
            self.group,
            self.version,
            namespace or self.namespace,
            self.plural,
        )
        return list(response.get("items", []))


class Dispatcher:
    def __init__(
        self,
        *,
        session_factory,
        cr_backend: CRBackend,
        namespace: str = DEFAULT_CELL_NAMESPACE,
        batch_size: int = 10,
    ):
        self._factory = session_factory
        self._cr = cr_backend
        self.namespace = namespace
        self.batch_size = batch_size

    def run_once(self) -> int:
        """Process up to batch_size outbox rows; returns how many were handled."""
        processed = 0
        with self._factory() as db:
            rows = db.scalars(
                select(OutboxRow)
                .where(OutboxRow.processed_at.is_(None))
                .order_by(OutboxRow.id)
                .limit(self.batch_size)
            ).all()
            for row in rows:
                try:
                    self._handle(db, row)
                    complete_outbox(db, row.id)
                    processed += 1
                except Exception:
                    logger.exception(
                        "dispatcher failed for outbox %s (%s)", row.id, row.event_type,
                    )
                    # Leave unprocessed; bounded by worker restarts + attempts col.
            self._sync_statuses(db)
            db.commit()
        return processed

    def run_forever(self, interval_seconds: float = 2.0) -> None:
        while True:
            try:
                self.run_once()
            except Exception:
                logger.exception("dispatcher cycle failed")
            time.sleep(interval_seconds)

    # -- handlers ----------------------------------------------------------

    def _handle(self, db: Session, row: OutboxRow) -> None:
        if row.event_type == "run.accepted":
            self._handle_run_accepted(db, row)
        elif row.event_type == "attempt.created":
            self._handle_attempt_created(db, row)
        elif row.event_type == "run.cancel_requested":
            self._handle_cancel(db, row)
        else:
            # Unknown/terminal events are drainable (already handled elsewhere).
            logger.warning("dispatcher ignoring unknown outbox event %s", row.event_type)

    def _handle_run_accepted(self, db: Session, row: OutboxRow) -> None:
        run = _get_run_unscoped(db, row.run_id)
        if run is None:
            raise RuntimeError(f"run {row.run_id} not found for outbox {row.id}")
        # Idempotent: only the first accepted event may create the first attempt.
        if run.current_attempt_id is None:
            new_attempt(db, run, reason="dispatcher:first-attempt")

    def _handle_attempt_created(self, db: Session, row: OutboxRow) -> None:
        import json

        attempt_id = (row.payload and json.loads(row.payload).get("attemptId")) or None
        if attempt_id is None:
            raise RuntimeError(f"attempt.created outbox {row.id} has no attemptId")
        attempt = get_attempt(db, attempt_id)
        if attempt is None:
            raise RuntimeError(f"attempt {attempt_id} not found for outbox {row.id}")
        run = _get_run_unscoped(db, row.run_id)
        if run is None:
            raise RuntimeError(f"run {row.run_id} not found for attempt {attempt_id}")

        manifest = self._build_manifest(run, attempt)
        self._cr.apply_sandboxtask(manifest)
        if run.phase == RunPhase.ACCEPTED:
            transition_run(db, run, RunPhase.QUEUED, reason="dispatcher", message="SandboxTask CR created")

    def _handle_cancel(self, db: Session, row: OutboxRow) -> None:
        run = _get_run_unscoped(db, row.run_id)
        if run is None:
            raise RuntimeError(f"run {row.run_id} not found for cancel outbox {row.id}")
        if run.current_attempt_id:
            # Name is derived deterministically from run+attempt (see below).
            attempt = get_attempt(db, run.current_attempt_id)
            if attempt is not None:
                name = _cr_name(run, attempt)
                self._cr.cancel_sandboxtask(name, self.namespace)

    def _build_manifest(self, run: Run, attempt) -> dict[str, Any]:
        execution_spec = {
            "workerImage": os.environ.get(
                "CLAWBOX_WORKER_IMAGE", "clawbox/experiment-worker@sha256:" + "0" * 64,
            ),
            "cubeApiURL": os.environ.get("CUBE_API_URL", "http://cube-api.cube-system.svc:3000"),
            "resultHostPath": os.path.join(
                os.environ.get("CLAWBOX_RESULT_HOST_ROOT", "/data/clawbox-results"),
                run.run_id, attempt.attempt_id,
            ),
            "timeoutSeconds": min(86400, max(60, run.deadline_seconds)),
            "experimentSpec": run.experiment_spec,
        }
        if secret := os.environ.get("CLAWBOX_WORKER_SECRET"):
            execution_spec["credentialSecretName"] = secret
        annotations = {
            "clawbox.openai.com/input-ref": run.input_ref,
            "clawbox.openai.com/template": f"{run.template_ref}@{run.template_revision}",
        }
        return build_sandboxtask_v1alpha2(
            name=_cr_name(run, attempt),
            namespace=self.namespace,
            tenant_id=run.tenant_id,
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            idempotency_key=run.idempotency_key,
            request_digest=run.request_digest,
            desired_state=(
                DESIRED_CANCELLED if run.desired_state == "Cancelled" else DESIRED_RUNNING
            ),
            execution_spec=execution_spec,
            labels={
                "app.kubernetes.io/name": "clawbox-managed",
                "app.kubernetes.io/managed-by": "clawbox-dispatcher",
                "clawbox.openai.com/tenant": _dns_label(run.tenant_id),
                "clawbox.openai.com/run": _dns_label(run.run_id),
            },
            annotations=annotations,
        )

    def _sync_statuses(self, db: Session) -> None:
        list_tasks = getattr(self._cr, "list_sandboxtasks", None)
        if list_tasks is None:
            return
        for task in list_tasks(self.namespace):
            try:
                self._sync_status(db, task)
            except Exception:
                logger.exception(
                    "failed to synchronize SandboxTask status for %s",
                    task.get("metadata", {}).get("name", "unknown"),
                )

    def _sync_status(self, db: Session, task: dict[str, Any]) -> None:
        spec = task.get("spec") or {}
        run_ref = spec.get("runRef") or {}
        run_id = run_ref.get("runID")
        attempt_id = run_ref.get("attemptID")
        status = task.get("status") or {}
        observed = status.get("phase")
        if not run_id or not attempt_id or not observed:
            return
        run = _get_run_unscoped(db, str(run_id))
        attempt = get_attempt(db, str(attempt_id))
        if run is None or attempt is None or attempt.run_id != run.run_id:
            return
        # Historical SandboxTasks remain in the namespace after a retry. They
        # may still report their terminal status, but only the current attempt
        # is allowed to project the user-visible Run lifecycle.
        if run.current_attempt_id != attempt.attempt_id:
            return

        if observed == "cleaned":
            observed = "cancelled" if status.get("errorCategory") == "cancelled" else "failed"
        if status.get("resultRef"):
            attempt.result_manifest_ref = str(status["resultRef"])
        attempt_target = _attempt_phase_for_cell(observed)
        run_target = _run_phase_for_cell(observed)
        if attempt_target is None or run_target is None:
            return

        if attempt_target == AttemptPhase.SUCCEEDED:
            attempt.platform_outcome = PlatformOutcome.SUCCEEDED
            attempt.agent_outcome = AgentOutcome.SUCCEEDED
            attempt.artifact_outcome = ArtifactOutcome.COMPLETE
        elif attempt_target == AttemptPhase.FAILED:
            attempt.platform_outcome = PlatformOutcome.FAILED
            attempt.agent_outcome = AgentOutcome.FAILED
        elif attempt_target == AttemptPhase.TIMED_OUT:
            attempt.platform_outcome = PlatformOutcome.INTERRUPTED
            attempt.agent_outcome = AgentOutcome.TIMED_OUT
        elif attempt_target == AttemptPhase.CANCELLED:
            attempt.platform_outcome = PlatformOutcome.INTERRUPTED
            attempt.agent_outcome = AgentOutcome.CANCELLED

        _advance_attempt(db, attempt, attempt_target, status)
        _advance_run(db, run, run_target, status)


# -- helpers ---------------------------------------------------------------

def _get_run_unscoped(db: Session, run_id: str) -> Run | None:
    from clawbox.managed.db import RunRow
    from clawbox.managed.repo import _row_to_run  # noqa: PLC2701 (internal mapping)

    row = db.get(RunRow, run_id)
    return _row_to_run(row) if row else None


def _cr_name(run: Run, attempt) -> str:
    # Deterministic, DNS-safe, <= 48 chars (CRD rule + owned child names).
    # ULIDs are Crockford base32 (uppercase); RFC 1123 requires lowercase, so
    # the name carries the lowercased run id (the runRef keeps the exact id).
    return f"run-{run.run_id.lower()}-a{attempt.attempt_number}"


def _problem_statement(run: Run) -> str:
    # Full problem text when the API provided it (real agent tasks); otherwise
    # input_ref is a display/registry reference (smoke/placeholder runs).
    return run.problem_statement or run.input_ref


_ATTEMPT_PROGRESS = [
    AttemptPhase.PENDING_DISPATCH,
    AttemptPhase.QUEUED,
    AttemptPhase.ADMITTED,
    AttemptPhase.TOOL_STARTING,
    AttemptPhase.TOOL_READY,
    AttemptPhase.RUNTIME_RUNNING,
    AttemptPhase.COLLECTING,
]
_RUN_PROGRESS = [
    RunPhase.ACCEPTED,
    RunPhase.QUEUED,
    RunPhase.RUNNING,
    RunPhase.FINALIZING,
]


def _attempt_phase_for_cell(phase: str | None) -> AttemptPhase | None:
    return {
        "pending": AttemptPhase.QUEUED,
        "running": AttemptPhase.RUNTIME_RUNNING,
        "succeeded": AttemptPhase.SUCCEEDED,
        "failed": AttemptPhase.FAILED,
        "cancelled": AttemptPhase.CANCELLED,
    }.get(phase or "")


def _run_phase_for_cell(phase: str | None) -> RunPhase | None:
    mapping = {
        "pending": RunPhase.QUEUED,
        "running": RunPhase.RUNNING,
        "succeeded": RunPhase.SUCCEEDED,
        "failed": RunPhase.FAILED,
        "cancelled": RunPhase.CANCELLED,
    }
    return mapping.get(phase or "")


def _status_detail(status: dict[str, Any]) -> tuple[str | None, str | None]:
    return status.get("errorCategory"), None


def _advance_attempt(
    db: Session,
    attempt,
    target: AttemptPhase,
    status: dict[str, Any],
) -> None:
    if attempt.phase == target:
        return
    if attempt.is_terminal:
        return
    reason, message = _status_detail(status)
    if target in _ATTEMPT_PROGRESS:
        current = _ATTEMPT_PROGRESS.index(attempt.phase)
        desired = _ATTEMPT_PROGRESS.index(target)
        for phase in _ATTEMPT_PROGRESS[current + 1 : desired + 1]:
            transition_attempt(db, attempt, phase, reason=reason, message=message)
        return
    if target == AttemptPhase.SUCCEEDED:
        # A Dispatcher restart or a fast Cell can make the first observation a
        # terminal success. Walk the required state-machine path before the
        # terminal transition instead of retrying an illegal direct jump.
        current = _ATTEMPT_PROGRESS.index(attempt.phase)
        for phase in _ATTEMPT_PROGRESS[current + 1 :]:
            transition_attempt(db, attempt, phase, reason=reason, message=message)
    transition_attempt(db, attempt, target, reason=reason, message=message)


def _advance_run(db: Session, run: Run, target: RunPhase, status: dict[str, Any]) -> None:
    if run.phase == target:
        return
    if run.is_terminal:
        return
    reason, message = _status_detail(status)
    if target in _RUN_PROGRESS:
        current = _RUN_PROGRESS.index(run.phase)
        desired = _RUN_PROGRESS.index(target)
        for phase in _RUN_PROGRESS[current + 1 : desired + 1]:
            transition_run(db, run, phase, reason=reason, message=message)
        return
    if target == RunPhase.SUCCEEDED:
        # Success is only legal from Finalizing. Reconstruct any missed
        # progressive phases so terminal status remains restart-safe.
        current = _RUN_PROGRESS.index(run.phase)
        for phase in _RUN_PROGRESS[current + 1 :]:
            transition_run(db, run, phase, reason=reason, message=message)
    transition_run(db, run, target, reason=reason, message=message)
