from threading import Event, RLock, Thread

from clawbox.cube.lifecycle import CubeSandboxLifecycle, SandboxState


def test_resident_observation_does_not_wait_for_lifecycle_operation():
    lifecycle = object.__new__(CubeSandboxLifecycle)
    lifecycle._lock = RLock()
    lifecycle._state = SandboxState.RUNNING
    observed = Event()
    result = []
    def read():
        result.append(lifecycle.resident)
        observed.set()
    with lifecycle._lock:
        reader = Thread(target=read, daemon=True)
        reader.start()
        completed_without_release = observed.wait(1)
    reader.join(2)
    assert completed_without_release
    assert result == [True]
    for state in SandboxState:
        lifecycle._state = state
        assert lifecycle.resident == (state is SandboxState.RUNNING)
