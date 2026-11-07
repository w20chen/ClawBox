"""Exercise the worker's actual completion callback with a blocked admission."""
import ast
import pytest
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace

from clawbox.experiments.worker import (
    ExperimentWorker, RuntimeStatusPoller, wait_for_admission_without_lifecycle_lock,
)
from clawbox.replay.lifecycle import CommandResult
from clawbox.experiments.prediction_records import PredictionRecordBuffer


def test_concurrent_admission_cannot_use_pre_restore_route_during_memory_wait():
    lifecycle_lock = Lock()
    waiting = Event()
    release = Event()
    lifecycle = SimpleNamespace(resident=False)
    cached_route = {'port': 2222, 'bridge_ready': False}
    coordinator = SimpleNamespace(
        set_tool_active=lambda *args: None,
        begin_tool_admission=lambda *args, **kwargs: 0.0,
    )
    arm = SimpleNamespace(execution=SimpleNamespace(arm_timeout_seconds=30))
    worker = object.__new__(ExperimentWorker)
    worker._restore_with_one_victim = lambda *args: setattr(lifecycle, 'resident', True)
    errors = []

    def restored():
        assert lifecycle.resident
        assert lifecycle_lock.locked()
        cached_route.update(port=None, bridge_ready=True)

    def reserve_after_concurrent_admission(operation):
        def reserve():
            waiting.set()
            assert release.wait(2)
            return operation()
        return wait_for_admission_without_lifecycle_lock(lifecycle_lock, reserve)

    def first_admission():
        try:
            with lifecycle_lock:
                worker._prepare_tool_admission(
                    arm, 'session', lifecycle, coordinator, SimpleNamespace(), None, 3,
                    on_restored=restored,
                    wait_for_admission=reserve_after_concurrent_admission,
                )
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=first_admission, daemon=True)
    thread.start()
    try:
        assert waiting.wait(2)
        with lifecycle_lock:
            # A second callback sees the same sandbox ID, now resident. It must
            # find no cached old route even while the restoring callback waits.
            worker._prepare_tool_admission(
                arm, 'session', lifecycle, coordinator, SimpleNamespace(), None, 3,
                on_restored=lambda: pytest.fail('resident VM restored twice'),
                wait_for_admission=lambda operation: operation(),
            )
            assert cached_route == {'port': None, 'bridge_ready': True}
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert not errors


def test_resident_runtime_poll_does_not_take_model_lifecycle_lock():
    lifecycle_lock = Lock()
    lifecycle_lock.acquire()
    executor = SimpleNamespace(
        execute=lambda command, timeout: CommandResult(0, command, "", 0.0),
    )
    poller = RuntimeStatusPoller(
        lifecycle=SimpleNamespace(resident=True), executor=executor,
        lifecycle_lock=lifecycle_lock, snapshot_enabled=False,
    )
    completed = Event()
    result = []

    thread = Thread(
        target=lambda: (result.append(poller("status", 1)), completed.set()),
        daemon=True,
    )
    thread.start()
    try:
        assert completed.wait(1), "resident status polling must not block model delivery"
    finally:
        lifecycle_lock.release()
        thread.join(1)
    assert result[0].stdout == "status"


@pytest.mark.parametrize("native_stat", [False, True])
def test_snapshot_runtime_poll_serializes_with_lifecycle_transition(native_stat):
    lifecycle_lock = Lock()
    lifecycle_lock.acquire()
    executor = SimpleNamespace(
        execute=lambda command, timeout: CommandResult(0, command, "", 0.0),
        file_nonempty=lambda path, timeout: True,
    )
    poller = RuntimeStatusPoller(
        lifecycle=SimpleNamespace(resident=True), executor=executor,
        lifecycle_lock=lifecycle_lock, snapshot_enabled=True,
    )
    completed = Event()
    probe = poller.file_nonempty if native_stat else poller
    thread = Thread(target=lambda: (probe("status", 1), completed.set()), daemon=True)
    thread.start()
    assert not completed.wait(0.05)
    lifecycle_lock.release()
    thread.join(1)
    assert completed.is_set()


def test_native_runtime_file_poll_skips_nonresident_guest():
    lifecycle_lock = Lock()
    poller = RuntimeStatusPoller(
        lifecycle=SimpleNamespace(resident=False),
        executor=SimpleNamespace(file_nonempty=lambda *_: pytest.fail("guest is paused")),
        lifecycle_lock=lifecycle_lock, snapshot_enabled=True,
    )
    assert poller.file_nonempty("/state/agent.exit", 10) is None
    assert lifecycle_lock.acquire(blocking=False)
    lifecycle_lock.release()


@pytest.mark.parametrize("stream_failure", [False, True])
def test_completion_watch_releases_lifecycle_lock_and_discards_old_generation(stream_failure):
    lifecycle_lock = Lock()
    lifecycle = SimpleNamespace(resident=True, sandbox=object())
    old = lifecycle.sandbox

    def wait(sandbox, path, **kwargs):
        assert sandbox is old
        assert lifecycle_lock.acquire(blocking=False), "watch must not prevent checkpoint"
        try:
            lifecycle.sandbox = object()
        finally:
            lifecycle_lock.release()
        if stream_failure:
            raise ConnectionError("old VM was checkpointed")
        return True

    poller = RuntimeStatusPoller(
        lifecycle=lifecycle, lifecycle_lock=lifecycle_lock, snapshot_enabled=True,
        executor=SimpleNamespace(client=SimpleNamespace(wait_file_nonempty=wait)),
    )
    assert poller.watch_file_nonempty("/state/completion/agent.exit", 10) is None


def test_resident_runtime_wait_uses_one_remote_command_without_lifecycle_lock():
    lifecycle_lock = Lock()
    lifecycle_lock.acquire()
    calls = []

    def execute(command, timeout):
        calls.append((command, timeout))
        return CommandResult(0, "", "", 0.0)

    poller = RuntimeStatusPoller(
        lifecycle=SimpleNamespace(resident=True),
        executor=SimpleNamespace(execute=execute),
        lifecycle_lock=lifecycle_lock,
        snapshot_enabled=False,
    )
    completed = Event()
    result = []
    thread = Thread(
        target=lambda: (
            result.append(poller.wait_for_file("/state/agent exit", 60)),
            completed.set(),
        ),
        daemon=True,
    )
    thread.start()
    try:
        assert completed.wait(1), "resident wait must not take the lifecycle lock"
    finally:
        lifecycle_lock.release()
        thread.join(1)

    assert result[0].exit_code == 0
    assert calls == [(
        "while [ ! -s '/state/agent exit' ]; do sleep 1; done", 60,
    )]


def test_snapshot_runtime_rejects_unserialized_resident_wait():
    poller = RuntimeStatusPoller(
        lifecycle=SimpleNamespace(resident=True),
        executor=SimpleNamespace(execute=lambda *_args: None),
        lifecycle_lock=Lock(), snapshot_enabled=True,
    )
    with pytest.raises(RuntimeError, match="snapshot-enabled"):
        poller.wait_for_file("/state/agent.exit", 60)


def test_blocked_admission_does_not_block_tool_completion():
    lifecycle_lock = Lock()
    admission_started = Event()
    release_admission = Event()
    completion_finished = Event()

    def admission():
        admission_started.set()
        assert release_admission.wait(2)
        return 1.25

    def run_admission():
        with lifecycle_lock:
            assert wait_for_admission_without_lifecycle_lock(
                lifecycle_lock, admission,
            ) == 1.25

    thread = Thread(target=run_admission, daemon=True)
    thread.start()
    assert admission_started.wait(1)
    with lifecycle_lock:
        completion_finished.set()
    assert completion_finished.is_set(), (
        "Tool completion must acquire the lifecycle lock while admission waits"
    )
    release_admission.set()
    thread.join(1)
    assert not thread.is_alive()


@pytest.mark.parametrize('runtime_enabled', [False, True])
@pytest.mark.parametrize('another_pending', [False, True])
def test_completion_releases_memory_before_waiting_for_lifecycle_lock(runtime_enabled, another_pending):
    from clawbox.experiments.control_timing import span, timed_lock, trace_callback
    source = Path(__file__).resolve().parents[1] / 'clawbox/experiments/worker.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    callback = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == 'complete_openclaw_tool')
    callback.returns = None
    for arg in callback.args.args:
        arg.annotation = None
    module = ast.fix_missing_locations(ast.Module(body=[callback], type_ignores=[]))
    released = Event()
    wait_lock = Lock()
    idle_updates = []
    runtime_released = Event()
    route = SimpleNamespace(sandbox_id='tool', epoch=1, host='host', port=2222)
    environment = dict(
        span=span, timed_lock=timed_lock, trace_callback=trace_callback,
        math=__import__('math'), wait_lock=wait_lock, reservation_lock=Lock(),
        active_reservations={'exec': (512, 512)}, admitted_routes={'exec': route},
            active_cpu_placements=set(), priority_domain=None,
            boundary_study=None,
        pending_admissions={'next'} if another_pending else set(),
        progress_control=None,
        dependency_install_gate=None,
        host_rss_samplers={'exec': SimpleNamespace(stop=lambda: {})},
        prediction_buffer=PredictionRecordBuffer(),
        lifecycle=SimpleNamespace(complete_first_tool_after_restore=lambda *args, **kwargs: None),
        coordinator=SimpleNamespace(release=lambda *args, **kwargs: released.set(),
                                    set_tool_active=lambda *args: idle_updates.append(args)),
        session_id='session', timeline={}, _record_time_span=lambda *args, **kwargs: None,
        self=SimpleNamespace(_runtime_memory=(SimpleNamespace(
            complete=lambda key: runtime_released.set()) if runtime_enabled else None)),
        events=SimpleNamespace(write=lambda value: None),
    )
    exec(compile(module, str(source), 'exec'), environment)
    request = dict(execution_id='exec', execution_started_at=1, ssh_reaped_at=2,
                   execution_completed_at=3, endpoint_sandbox_id='tool', endpoint_epoch=1,
                   endpoint_host='host', endpoint_port=2222, exit_code=0)
    errors = []
    def complete():
        try:
            environment['complete_openclaw_tool'](request)
        except Exception as exc:
            errors.append(exc)
    wait_lock.acquire()
    thread = Thread(target=complete, daemon=True)
    thread.start()
    try:
        assert released.wait(2), 'Completion cannot free the memory needed by admission'
        assert idle_updates == [], 'Do not allow eviction during another admission'
        if another_pending:
            thread.join(2)
            assert not thread.is_alive(), 'Do not wait for another request to publish active again'
    finally:
        wait_lock.release()
        thread.join(2)
    assert not thread.is_alive()
    assert not errors
    assert not runtime_released.is_set()  # Runtime control owns no execution tickets.
    assert idle_updates == ([] if another_pending else [('session', False)])
