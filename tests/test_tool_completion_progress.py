"""Exercise the worker's actual completion callback with a blocked admission."""
import ast
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace

from clawbox.experiments.worker import RuntimeStatusPoller
from clawbox.replay.lifecycle import CommandResult


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


def test_snapshot_runtime_poll_serializes_with_lifecycle_transition():
    lifecycle_lock = Lock()
    lifecycle_lock.acquire()
    executor = SimpleNamespace(
        execute=lambda command, timeout: CommandResult(0, command, "", 0.0),
    )
    poller = RuntimeStatusPoller(
        lifecycle=SimpleNamespace(resident=True), executor=executor,
        lifecycle_lock=lifecycle_lock, snapshot_enabled=True,
    )
    completed = Event()
    thread = Thread(target=lambda: (poller("status", 1), completed.set()), daemon=True)
    thread.start()
    assert not completed.wait(0.05)
    lifecycle_lock.release()
    thread.join(1)
    assert completed.is_set()


def test_completion_releases_memory_before_waiting_for_lifecycle_lock():
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
    route = SimpleNamespace(sandbox_id='tool', epoch=1, host='host', port=2222)
    environment = dict(
        math=__import__('math'), wait_lock=wait_lock, reservation_lock=Lock(),
        active_reservations={'exec': 512}, admitted_routes={'exec': route},
        host_rss_samplers={'exec': SimpleNamespace(stop=lambda: {})}, prediction_records=[],
        lifecycle=SimpleNamespace(complete_first_tool_after_restore=lambda *args, **kwargs: None),
        coordinator=SimpleNamespace(release=lambda *args: released.set(),
                                    set_tool_active=lambda *args: idle_updates.append(args)),
        session_id='session', timeline={}, _record_time_span=lambda *args, **kwargs: None,
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
    finally:
        wait_lock.release()
        thread.join(2)
    assert not thread.is_alive()
    assert not errors
    assert idle_updates == [('session', False)]
