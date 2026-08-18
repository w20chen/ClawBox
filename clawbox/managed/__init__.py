"""Managed agent sandbox control-plane contracts (M1).

Holds the server-generated identity, Run/Attempt/Execution state machines and
outcome model that the Managed API, PostgreSQL, and SandboxTask CRD v1alpha2
all conform to (ADR-002, ADR-003, roadmap §6.2 and §14.5).

Nothing here touches Kubernetes or PostgreSQL directly; it is the pure contract
layer so every component can be tested against the same invariants.
"""

from clawbox.managed.ids import new_attempt_id, new_execution_id, new_run_id, new_ulid
from clawbox.managed.models import (
    AgentOutcome,
    ArtifactOutcome,
    Attempt,
    AttemptPhase,
    EvaluationOutcome,
    PlatformOutcome,
    Run,
    RunEvent,
    RunIntent,
    RunPhase,
    idempotency_digest,
)
from clawbox.managed.state import (
    ATTEMPT_TRANSITIONS,
    RUN_TRANSITIONS,
    attempt_can_cancel,
    attempt_transition,
    is_terminal_attempt,
    is_terminal_run,
    run_can_cancel,
    run_transition,
)

__all__ = [
    "AgentOutcome", "ArtifactOutcome", "Attempt", "AttemptPhase",
    "ATTEMPT_TRANSITIONS", "EvaluationOutcome", "PlatformOutcome", "Run",
    "RunEvent", "RunIntent", "RunPhase", "RUN_TRANSITIONS",
    "attempt_can_cancel", "attempt_transition", "idempotency_digest",
    "is_terminal_attempt", "is_terminal_run", "new_attempt_id",
    "new_execution_id", "new_run_id", "new_ulid", "run_can_cancel",
    "run_transition",
]
