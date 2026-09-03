"""Managed control-plane data models (ADR-002, roadmap §6.2 / §14.5).

Pure Python dataclasses; the PostgreSQL schema and the API schema are mapped
from these in later M1 phases.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from clawbox.managed.ids import new_attempt_id, new_run_id
from clawbox.managed.state import AttemptPhase, RunPhase


# --------------------------------------------------------------------------
# Outcomes (roadmap §14.5): separated so a platform contract can succeed while
# the agent/artifact/evaluation did not, and each is auditable independently.
# --------------------------------------------------------------------------

class PlatformOutcome(str, Enum):
    PENDING = "Pending"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    INTERRUPTED = "Interrupted"


class AgentOutcome(str, Enum):
    PENDING = "Pending"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    TIMED_OUT = "TimedOut"
    CANCELLED = "Cancelled"
    UNKNOWN = "Unknown"


class ArtifactOutcome(str, Enum):
    PENDING = "Pending"
    COMPLETE = "Complete"
    PARTIAL = "Partial"
    FINALIZATION_PENDING = "FinalizationPending"
    FAILED = "Failed"


class EvaluationOutcome(str, Enum):
    NOT_RUN = "NotRun"
    PASSED = "Passed"
    FAILED = "Failed"
    ERROR = "Error"


# --------------------------------------------------------------------------
# Idempotency (ADR-002)
# --------------------------------------------------------------------------

def idempotency_digest(payload: dict) -> str:
    """Stable sha256 of a canonicalized request body.

    Used as the `(tenant, idempotency_key)` match: identical bodies yield the
    same digest, so a retry with the same key returns the original Run while a
    different body with the same key is a 409.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Run / Attempt / Event
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RunIntent:
    """The normalized request the API accepts (tenant-scoped)."""

    tenant_id: str
    project_id: str
    template_ref: str
    template_revision: int
    input_ref: str
    input_sha256: str
    deadline_seconds: int
    idempotency_key: str
    # sha256 of the canonical request body; the idempotency match key.
    request_digest: str
    # Optional full problem text (otherwise input_ref is used as the problem).
    problem_statement: str | None = None
    experiment_spec: dict = field(default_factory=dict)


@dataclass
class Run:
    run_id: str
    tenant_id: str
    project_id: str
    template_ref: str
    template_revision: int
    input_ref: str
    input_sha256: str
    deadline_seconds: int
    idempotency_key: str
    request_digest: str
    # Optional full problem text (otherwise input_ref is used as the problem).
    problem_statement: str | None = None
    experiment_spec: dict = field(default_factory=dict)
    phase: RunPhase = RunPhase.ACCEPTED
    # One-way desired state (e.g. "Cancelled"); nil until a cancel is recorded.
    desired_state: str | None = None
    current_attempt_id: str | None = None
    attempt_counter: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    committed_at: str | None = None
    cancel_requested_at: str | None = None
    final_reason: str | None = None

    @staticmethod
    def new(intent: RunIntent, now: str) -> "Run":
        return Run(
            run_id=new_run_id(),
            tenant_id=intent.tenant_id,
            project_id=intent.project_id,
            template_ref=intent.template_ref,
            template_revision=intent.template_revision,
            input_ref=intent.input_ref,
            input_sha256=intent.input_sha256,
            deadline_seconds=intent.deadline_seconds,
            idempotency_key=intent.idempotency_key,
            request_digest=intent.request_digest,
            problem_statement=intent.problem_statement,
            experiment_spec=dict(intent.experiment_spec),
            created_at=now,
            updated_at=now,
        )

    @property
    def is_terminal(self) -> bool:
        from clawbox.managed.state import is_terminal_run

        return is_terminal_run(self.phase)

    def new_attempt(self, now: str) -> "Attempt":
        """Create the next Attempt for this Run (retry semantics, ADR-002)."""
        if self.is_terminal and self.phase != RunPhase.FAILED:
            raise ValueError(
                f"cannot retry terminal Run in phase {self.phase.value}"
            )
        if self.phase == RunPhase.FAILED:
            self.phase = RunPhase.ACCEPTED
            self.committed_at = None
            self.final_reason = None
        self.attempt_counter += 1
        attempt = Attempt(
            attempt_id=new_attempt_id(),
            run_id=self.run_id,
            attempt_number=self.attempt_counter,
            created_at=now,
            updated_at=now,
        )
        self.current_attempt_id = attempt.attempt_id
        self.updated_at = now
        return attempt


@dataclass
class Attempt:
    attempt_id: str
    run_id: str
    attempt_number: int
    phase: AttemptPhase = AttemptPhase.PENDING_DISPATCH
    platform_outcome: PlatformOutcome = PlatformOutcome.PENDING
    agent_outcome: AgentOutcome = AgentOutcome.PENDING
    artifact_outcome: ArtifactOutcome = ArtifactOutcome.PENDING
    evaluation_outcome: EvaluationOutcome = EvaluationOutcome.NOT_RUN
    desired_state: str | None = None
    cancel_requested_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # Never reused across attempts; every retry gets a fresh workspace/blob set.
    result_manifest_ref: str | None = None
    # Race evidence preserved when cancel intent and terminal commit collide.
    cancel_after_terminal: bool = False
    final_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        from clawbox.managed.state import is_terminal_attempt

        return is_terminal_attempt(self.phase)

    def record_cancel(self, now: str) -> None:
        """One-way cancel desired state; keeps evidence if already terminal."""
        if self.is_terminal:
            self.cancel_after_terminal = True
            self.final_reason = "cancel-received-after-terminal-commit"
            return
        self.desired_state = "Cancelled"
        self.cancel_requested_at = now
        self.updated_at = now

    def can_commit_platform_success(self) -> bool:
        """Platform contract Succeeded requires the artifact manifest Complete
        (ADR-004 strong receipt); a partial manifest cannot claim success."""
        return self.artifact_outcome == ArtifactOutcome.COMPLETE


@dataclass
class RunEvent:
    """Append-only event (stored in run_events / outbox in M1-3)."""

    sequence: int
    run_id: str
    attempt_id: str | None
    event_type: str
    phase: str
    reason: str | None
    message: str | None
    observed_generation: int
    last_transition_time: str | None = None
    payload: dict = field(default_factory=dict)
