from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from clawbox.experiments.worker import (
    ArmInfrastructureError, collect_session_results, classify_arm_failure,
)
from clawbox.experiments.results import FailureCategory


@pytest.mark.parametrize("error", [
    RuntimeError("guest OOM"), TimeoutError("command deadline"),
    ValueError("agent failed"),
])
def test_failed_agent_does_not_cancel_running_or_queued_siblings(error):
    failed_observed, healthy_started = Event(), Event()
    records, sessions, failures = [], [], []

    def healthy():
        healthy_started.set()
        assert failed_observed.wait(2)
        return {"valid": True, "agent": "running"}

    def fail():
        assert healthy_started.wait(2)
        raise error

    def emit(record):
        records.append(record)
        failed_observed.set()

    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(healthy)
        failed = pool.submit(fail)
        queued = pool.submit(lambda: {"valid": True, "agent": "queued"})
        result = collect_session_results(
            {running: 0, failed: 1, queued: 2}, timeout=3,
            sessions=sessions, failures=failures,
            events=SimpleNamespace(write=emit), arm_id="arm",
            check_health=lambda: None,
        )
    assert result is error
    assert {s["agent"] for s in sessions} == {"running", "queued"}
    assert failures == records
    assert failures[0]["session_id"] == "arm-0001"
    assert not any(f.cancelled() for f in (running, failed, queued))


def test_incomplete_cleanup_escapes_for_arm_shutdown():
    failed, pending = Future(), Future()
    failed.set_exception(ArmInfrastructureError("session cleanup incomplete"))
    with pytest.raises(ArmInfrastructureError, match="cleanup incomplete"):
        collect_session_results(
            {failed: 0, pending: 1}, timeout=1, sessions=[], failures=[],
            events=SimpleNamespace(write=lambda record: None), arm_id="arm",
            check_health=lambda: None,
        )
    assert not pending.done()


def test_shared_controller_failure_escapes_for_arm_shutdown():
    failed, pending = Future(), Future()
    failed.set_exception(ValueError("local command failed"))
    def check():
        raise ArmInfrastructureError("shared policy controller failed")
    with pytest.raises(ArmInfrastructureError, match="shared policy"):
        collect_session_results(
            {failed: 0, pending: 1}, timeout=1, sessions=[], failures=[],
            events=SimpleNamespace(write=lambda record: None), arm_id="arm",
            check_health=check,
        )


def test_arm_deadline_still_stops_unfinished_agents():
    with pytest.raises(TimeoutError):
        collect_session_results(
            {Future(): 0}, timeout=.01, sessions=[], failures=[],
            events=SimpleNamespace(write=lambda record: None), arm_id="arm",
            check_health=lambda: None,
        )


def test_unknown_local_error_is_not_reported_as_shared_infrastructure_failure():
    assert classify_arm_failure(ValueError("bad response"), session_failure=True) == FailureCategory.AGENT
    assert classify_arm_failure(
        ValueError("bad response"), session_failure=True,
        infrastructure_error=ArmInfrastructureError("cleanup failed"),
    ) == FailureCategory.INFRASTRUCTURE
