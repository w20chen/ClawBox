"""Deprecated control-plane module; see docs/deprecated.md.

No historical compatibility guarantee. Shared current callers remain supported.
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
