import time

import pytest

from clawbox.experiments.runtime_completion import wait_runtime_completion


class Clock:
    now = 0.

    def wait(self, _phase, seconds):
        self.now += seconds
        return False


def test_completion_notification_wakes_without_idle_filesystem_requests(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('clawbox.experiments.runtime_completion.time.monotonic', lambda: clock.now)
    checks, events = [], []

    def wait(phase, seconds):
        assert phase == 'agent'
        clock.now += min(seconds, .2)
        return True

    wait_runtime_completion('agent', '/completion/agent.exit', wait=wait,
        probe=lambda path, timeout: checks.append((path, timeout)) or True,
        interval_seconds=10, deadline=20, event=events.append)
    assert clock.now == .2
    assert len(checks) == 1
    assert events[0]['notification_observed'] is True


def test_missing_notification_uses_bounded_file_reconciliation(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('clawbox.experiments.runtime_completion.time.monotonic', lambda: clock.now)
    checks, events = [], []

    def probe(*_):
        checks.append(clock.now)
        return len(checks) == 2

    wait_runtime_completion('agent', '/completion/agent.exit', wait=clock.wait,
        probe=probe, interval_seconds=10, deadline=30, event=events.append)
    assert checks == [10, 20]
    assert events[0]['notification_observed'] is False


def test_notification_cannot_replace_the_exit_file_or_spin_on_a_paused_vm(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('clawbox.experiments.runtime_completion.time.monotonic', lambda: clock.now)
    checks, notified = [], [True]

    def wait(phase, seconds):
        if notified:
            return notified.pop()
        return clock.wait(phase, seconds)

    def probe(*_):
        checks.append(clock.now)
        return None if len(checks) == 1 else True

    wait_runtime_completion('agent', '/completion/agent.exit', wait=wait,
        probe=probe, interval_seconds=10, deadline=30)
    assert checks == [0, 10]


@pytest.mark.parametrize('cancel', [False, True])
def test_completion_wait_preserves_deadline_and_cancellation(monkeypatch, cancel):
    clock = Clock()
    monkeypatch.setattr('clawbox.experiments.runtime_completion.time.monotonic', lambda: clock.now)
    with pytest.raises(InterruptedError if cancel else TimeoutError):
        wait_runtime_completion('agent', '/completion/agent.exit', wait=clock.wait,
            probe=lambda *_: pytest.fail('must stop before the first idle file check'),
            interval_seconds=10, deadline=3, cancelled=lambda: cancel and clock.now >= 1)
    assert clock.now == (1 if cancel else 3)
