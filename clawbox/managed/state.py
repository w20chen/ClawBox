"""Run/Attempt state machines (ADR-002, roadmap §6.2).

Run is the user-visible lifecycle; Attempt is the immutable execution attempt
(one internal SandboxTask); cancellation is a one-way desired state.

Invariants enforced here (and tested in tests/test_managed_state.py):
- terminal states are absorbing;
- cancellation is allowed from any non-terminal state and cannot be rolled back;
- a terminal commit cannot be overwritten by a later cancel, and vice versa
  (the first committed intent wins; the loser is preserved as race evidence).
"""

from __future__ import annotations

from enum import Enum


class RunPhase(str, Enum):
    ACCEPTED = "Accepted"
    QUEUED = "Queued"
    RUNNING = "Running"
    FINALIZING = "Finalizing"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    TIMED_OUT = "TimedOut"
    CANCELLED = "Cancelled"


class AttemptPhase(str, Enum):
    PENDING_DISPATCH = "PendingDispatch"
    QUEUED = "Queued"
    ADMITTED = "Admitted"
    TOOL_STARTING = "ToolStarting"
    TOOL_READY = "ToolReady"
    RUNTIME_RUNNING = "RuntimeRunning"
    COLLECTING = "Collecting"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    TIMED_OUT = "TimedOut"
    CANCELLED = "Cancelled"


_RUN_ABORTS = {RunPhase.FAILED, RunPhase.TIMED_OUT, RunPhase.CANCELLED}
RUN_TRANSITIONS: dict[RunPhase, set[RunPhase]] = {
    RunPhase.ACCEPTED: {RunPhase.QUEUED, *_RUN_ABORTS},
    RunPhase.QUEUED: {RunPhase.RUNNING, *_RUN_ABORTS},
    RunPhase.RUNNING: {RunPhase.FINALIZING, *_RUN_ABORTS},
    RunPhase.FINALIZING: {
        RunPhase.SUCCEEDED, RunPhase.FAILED, RunPhase.TIMED_OUT, RunPhase.CANCELLED,
    },
    # Terminal states are absorbing (no outgoing edges).
    RunPhase.SUCCEEDED: set(),
    RunPhase.FAILED: set(),
    RunPhase.TIMED_OUT: set(),
    RunPhase.CANCELLED: set(),
}

_ATTEMPT_ABORTS = {AttemptPhase.FAILED, AttemptPhase.TIMED_OUT, AttemptPhase.CANCELLED}
ATTEMPT_TRANSITIONS: dict[AttemptPhase, set[AttemptPhase]] = {
    AttemptPhase.PENDING_DISPATCH: {AttemptPhase.QUEUED, *_ATTEMPT_ABORTS},
    AttemptPhase.QUEUED: {AttemptPhase.ADMITTED, *_ATTEMPT_ABORTS},
    AttemptPhase.ADMITTED: {AttemptPhase.TOOL_STARTING, *_ATTEMPT_ABORTS},
    AttemptPhase.TOOL_STARTING: {AttemptPhase.TOOL_READY, *_ATTEMPT_ABORTS},
    AttemptPhase.TOOL_READY: {AttemptPhase.RUNTIME_RUNNING, *_ATTEMPT_ABORTS},
    AttemptPhase.RUNTIME_RUNNING: {AttemptPhase.COLLECTING, *_ATTEMPT_ABORTS},
    AttemptPhase.COLLECTING: {
        AttemptPhase.SUCCEEDED, AttemptPhase.FAILED, AttemptPhase.TIMED_OUT,
        AttemptPhase.CANCELLED,
    },
    AttemptPhase.SUCCEEDED: set(),
    AttemptPhase.FAILED: set(),
    AttemptPhase.TIMED_OUT: set(),
    AttemptPhase.CANCELLED: set(),
}

_TERMINAL_RUN = {RunPhase.SUCCEEDED, RunPhase.FAILED, RunPhase.TIMED_OUT, RunPhase.CANCELLED}
_TERMINAL_ATTEMPT = {
    AttemptPhase.SUCCEEDED, AttemptPhase.FAILED, AttemptPhase.TIMED_OUT, AttemptPhase.CANCELLED,
}


def is_terminal_run(phase: RunPhase) -> bool:
    return phase in _TERMINAL_RUN


def is_terminal_attempt(phase: AttemptPhase) -> bool:
    return phase in _TERMINAL_ATTEMPT


def run_transition(current: RunPhase, target: RunPhase) -> RunPhase:
    """Validate and return the target Run phase (idempotent if unchanged)."""
    if current == target:
        return target
    if target not in RUN_TRANSITIONS.get(current, set()):
        raise ValueError(f"illegal Run transition: {current.value} -> {target.value}")
    return target


def attempt_transition(current: AttemptPhase, target: AttemptPhase) -> AttemptPhase:
    if current == target:
        return target
    if target not in ATTEMPT_TRANSITIONS.get(current, set()):
        raise ValueError(
            f"illegal Attempt transition: {current.value} -> {target.value}"
        )
    return target


def run_can_cancel(phase: RunPhase) -> bool:
    return not is_terminal_run(phase)


def attempt_can_cancel(phase: AttemptPhase) -> bool:
    return not is_terminal_attempt(phase)
