"""M1-1 contract tests: identity, state machines, outcomes (ADR-002)."""

import pytest

from clawbox.managed.ids import is_ulid, new_attempt_id, new_execution_id, new_run_id, new_ulid
from clawbox.managed.models import (
    AgentOutcome,
    ArtifactOutcome,
    Attempt,
    AttemptPhase,
    PlatformOutcome,
    Run,
    RunIntent,
    RunPhase,
    idempotency_digest,
)
from clawbox.managed.state import (
    attempt_can_cancel,
    attempt_transition,
    is_terminal_attempt,
    is_terminal_run,
    run_can_cancel,
    run_transition,
)


def make_intent(**overrides) -> RunIntent:
    base = dict(
        tenant_id="tenant-a",
        project_id="proj-1",
        template_ref="swe-rebench-arm64",
        template_revision=3,
        input_ref="15five__scim2-filter-parser-13",
        input_sha256="a" * 64,
        deadline_seconds=1800,
        idempotency_key="key-1",
        request_digest="d" * 64,
    )
    base.update(overrides)
    return RunIntent(**base)


# ---------------------------------------------------------------------------
# IDs (ADR-002)
# ---------------------------------------------------------------------------

def test_ulid_format_and_collision_free():
    ids = {new_ulid() for _ in range(1000)}
    assert len(ids) == 1000
    for value in ids:
        assert is_ulid(value)
        assert len(value) == 26


def test_run_and_attempt_ids_are_ulids():
    assert is_ulid(new_run_id())
    assert is_ulid(new_attempt_id())


def test_execution_id_is_uuid():
    import uuid

    assert uuid.UUID(new_execution_id())


def test_ids_are_ordered_by_time():
    # ULID ordering is by timestamp prefix (first 10 chars); two IDs created in
    # the same millisecond share the prefix and are otherwise unordered.
    first = new_ulid()
    second = new_ulid()
    assert first[:10] <= second[:10]


# ---------------------------------------------------------------------------
# Run state machine
# ---------------------------------------------------------------------------

def test_run_happy_path_transitions():
    phase = RunPhase.ACCEPTED
    for target in (RunPhase.QUEUED, RunPhase.RUNNING, RunPhase.FINALIZING, RunPhase.SUCCEEDED):
        phase = run_transition(phase, target)
    assert phase == RunPhase.SUCCEEDED
    assert is_terminal_run(phase)


def test_run_illegal_transition_raises():
    with pytest.raises(ValueError):
        run_transition(RunPhase.ACCEPTED, RunPhase.RUNNING)  # skip Queued
    with pytest.raises(ValueError):
        run_transition(RunPhase.SUCCEEDED, RunPhase.RUNNING)  # terminal absorbing


def test_run_transition_same_phase_is_noop():
    assert run_transition(RunPhase.RUNNING, RunPhase.RUNNING) == RunPhase.RUNNING


def test_run_cancel_from_any_non_terminal():
    for phase in RunPhase:
        if is_terminal_run(phase):
            continue
        assert run_can_cancel(phase)
        assert run_transition(phase, RunPhase.CANCELLED) == RunPhase.CANCELLED


def test_run_cancel_is_absorbing():
    with pytest.raises(ValueError):
        run_transition(RunPhase.CANCELLED, RunPhase.FINALIZING)


# ---------------------------------------------------------------------------
# Attempt state machine
# ---------------------------------------------------------------------------

def test_attempt_happy_path_transitions():
    phase = AttemptPhase.PENDING_DISPATCH
    for target in (
        AttemptPhase.QUEUED,
        AttemptPhase.ADMITTED,
        AttemptPhase.TOOL_STARTING,
        AttemptPhase.TOOL_READY,
        AttemptPhase.RUNTIME_RUNNING,
        AttemptPhase.COLLECTING,
        AttemptPhase.SUCCEEDED,
    ):
        phase = attempt_transition(phase, target)
    assert phase == AttemptPhase.SUCCEEDED
    assert is_terminal_attempt(phase)


def test_attempt_illegal_skip_raises():
    with pytest.raises(ValueError):
        attempt_transition(AttemptPhase.QUEUED, AttemptPhase.RUNTIME_RUNNING)


def test_attempt_cancel_anywhere():
    for phase in AttemptPhase:
        if is_terminal_attempt(phase):
            continue
        assert attempt_can_cancel(phase)
        assert attempt_transition(phase, AttemptPhase.CANCELLED) == AttemptPhase.CANCELLED


# ---------------------------------------------------------------------------
# Run/Attempt model semantics (retry, cancel race)
# ---------------------------------------------------------------------------

def test_retry_creates_new_attempt_with_new_id():
    run = Run.new(make_intent(), now="2026-08-18T00:00:00Z")
    first = run.new_attempt(now="2026-08-18T00:00:01Z")
    second = run.new_attempt(now="2026-08-18T00:00:02Z")
    assert first.attempt_id != second.attempt_id
    assert first.attempt_number == 1
    assert second.attempt_number == 2
    assert run.attempt_counter == 2
    assert run.current_attempt_id == second.attempt_id
    # Result manifest never reused across attempts.
    assert first.result_manifest_ref is None
    assert second.result_manifest_ref is None


def test_retry_refuses_succeeded_run():
    run = Run.new(make_intent(), now="2026-08-18T00:00:00Z")
    run.phase = RunPhase.SUCCEEDED
    with pytest.raises(ValueError):
        run.new_attempt(now="2026-08-18T00:00:01Z")


def test_cancel_after_terminal_keeps_race_evidence():
    attempt = Attempt(attempt_id="x", run_id="r", attempt_number=1)
    attempt.phase = AttemptPhase.SUCCEEDED
    attempt.record_cancel(now="2026-08-18T00:00:05Z")
    assert attempt.cancel_after_terminal is True
    assert attempt.final_reason == "cancel-received-after-terminal-commit"
    # Terminal phase is not overwritten.
    assert attempt.phase == AttemptPhase.SUCCEEDED


def test_cancel_before_terminal_is_one_way():
    attempt = Attempt(attempt_id="x", run_id="r", attempt_number=1)
    attempt.record_cancel(now="2026-08-18T00:00:05Z")
    assert attempt.desired_state == "Cancelled"
    assert attempt.cancel_requested_at == "2026-08-18T00:00:05Z"
    # A later agent completion must NOT flip it back to Succeeded.
    with pytest.raises(ValueError):
        attempt_transition(attempt.phase, AttemptPhase.COLLECTING)


def test_platform_success_requires_complete_artifacts():
    attempt = Attempt(attempt_id="x", run_id="r", attempt_number=1)
    attempt.platform_outcome = PlatformOutcome.SUCCEEDED
    attempt.artifact_outcome = ArtifactOutcome.PARTIAL
    assert attempt.can_commit_platform_success() is False
    attempt.artifact_outcome = ArtifactOutcome.COMPLETE
    assert attempt.can_commit_platform_success() is True


def test_outcome_separation_defaults():
    attempt = Attempt(attempt_id="x", run_id="r", attempt_number=1)
    assert attempt.agent_outcome == AgentOutcome.PENDING
    assert attempt.platform_outcome == PlatformOutcome.PENDING
    assert attempt.artifact_outcome == ArtifactOutcome.PENDING


# ---------------------------------------------------------------------------
# Idempotency (ADR-002)
# ---------------------------------------------------------------------------

def test_idempotency_digest_is_canonical():
    a = idempotency_digest({"b": 1, "a": [1, 2], "c": None})
    b = idempotency_digest({"c": None, "b": 1, "a": [1, 2]})
    assert a == b
    assert len(a) == 64


def test_idempotency_digest_differs_on_body_change():
    assert idempotency_digest({"tool": "x"}) != idempotency_digest({"tool": "y"})
