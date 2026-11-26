from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from test_host_kb import setup_manager, prediction_payload
from test_gpu_policy import coordinator, waiting, signal, Lifecycle
from clawbox.experiments.subagent_drain import drained


def test_same_tool_id_in_distinct_sessions_and_runs_has_separate_receipts(tmp_path):
    manager = setup_manager(tmp_path)()
    manager.reserve('pair', 'repo')
    first = prediction_payload('pair')
    second = deepcopy(first)
    second['logical_owner']['session_id'] = 'child'
    third = deepcopy(first)
    third['logical_owner']['run_id'] = 'next-run'
    rows = [manager.predict('pair', row) for row in (first, second, third)]
    assert len({r['prediction_receipt'] for r in rows}) == 3
    assert manager.predict('pair', first) == rows[0]
    assert len(manager.scopes['pair'].predictions) == 3


def test_parallel_model_requests_keep_independent_gpu_state_and_tombstones():
    c = coordinator(snapshot_next_use_residency=True)
    waiting(c, rid='parent')
    c.begin_model_wait('session', 'child')
    assert signal(c, rid='parent', remaining_s=20, signal_sequence=1)['accepted']
    assert signal(c, rid='child', remaining_s=5, signal_sequence=1)['accepted']
    state = c._sessions['session']
    assert set(state.model_waits) == {'parent', 'child'}
    assert state.shared_runtime
    assert c._pair_response_remaining(state, __import__('time').monotonic()) == pytest.approx(5, abs=.1)
    assert signal(c, rid='parent', remaining_s=1., signal_sequence=2)['should_prewarm']
    assert c.wait_for_gpu_prewarm('session', 'parent')
    assert not c.wait_for_gpu_prewarm('session', 'child')
    c.complete_model_wait('session', 'parent')
    assert signal(c, rid='child', remaining_s=7, signal_sequence=2)['accepted']
    assert not signal(c, rid='parent', remaining_s=1., signal_sequence=3)['accepted']
    c.begin_model_wait('session', 'parent')
    assert set(state.model_waits) == {'child'}
    c.complete_model_wait('session', 'child')
    assert not state.model_waits


def test_shared_runtime_stays_resident_while_idle_tool_can_reclaim():
    class Counted(Lifecycle):
        def __init__(self):
            super().__init__()
            self.checkpoints = 0
        def checkpoint_and_evict(self, **_):
            self.checkpoints += 1
            self.resident = False
            return .01
    c = coordinator(runtime_snapshot=True)
    tool, runtime = Counted(), Counted()
    c.register('pair', tool)
    c.register_runtime('pair', runtime)
    c.begin_model_wait('pair', 'request')
    c.protect_shared_runtime('pair')
    signal(c, sid='pair', remaining_s=20)
    c.set_tool_active('pair', True)
    assert not c.evict_one_for_watermark()
    c.set_tool_active('pair', False)
    assert c.evict_one_for_watermark()
    assert tool.checkpoints == 1 and runtime.checkpoints == 0 and runtime.resident


def test_native_child_cleanup_and_parent_continuation_gate_completion():
    root = {'identity': {'session_id': 'root'}, 'payload': {'phase': 'IDLE'}}
    child = {'identity': {'session_id': 'child'}, 'payload': {'phase': 'TOOLS'}}
    assert not drained([root, child], {}, 'root')
    child['payload']['phase'] = 'IDLE'
    assert not drained([root, child], {'run': {'endedAt': 10}}, 'root')
    runs = {'run': {'endedAt': 10, 'cleanupCompletedAt': 11}}
    assert drained([root, child], runs, 'root')
    root['payload']['phase'] = 'WAIT_EVENT'
    assert not drained([root, child], runs, 'root')
    assert not drained([child], runs, 'root')


def test_subagent_config_requires_live_host_kb_and_explicit_runtime_residency():
    from test_unified_configuration import documents, compile_pair
    baseline, workload = documents()
    baseline['agent'] = {'subagents': True}
    with pytest.raises(ValueError, match='subagents requires OpenClaw'):
        compile_pair(baseline, workload)
    baseline['inference']['backend'] = 'api'
    with pytest.raises(ValueError, match='subagents requires host-live'):
        compile_pair(baseline, workload)
    baseline['conditions']['prediction_source'] = 'host-live'
    baseline['evidence']['prediction_artifact'] = '/tmp/validated-seed'
    spec = compile_pair(baseline, workload)
    assert spec.agent.subagents and spec.agent.subagent_max_spawn_depth == 1
    from clawbox.experiments.qualification import qualification_spec
    qualified = qualification_spec(spec, concurrency=1)
    assert qualified.agent.subagents
    assert qualified.inference.backend.value == 'api'
    assert 'max_model_steps' not in qualified.inference.configuration
    assert qualified.inference.configuration['qualification_max_parallel_sessions'] == 1
    baseline['mechanisms']['idle_reclaim']['scope'] = 'agent_and_tool'
    with pytest.raises(ValueError, match='scope=tool'):
        compile_pair(baseline, workload)


@pytest.mark.parametrize('fallback', [False, True])
@pytest.mark.parametrize('execution_mode', ['sync', 'detached', 'cancel_children'])
def test_gateway_mode_launches_native_subagents_and_drains_before_cleanup(monkeypatch, tmp_path, fallback, execution_mode):
    from test_openclaw_driver import PolicySession
    from clawbox.experiments.openclaw_driver import run_openclaw, NativeSSHConfig
    from clawbox.replay.lifecycle import CommandResult
    commands = []
    detached = execution_mode != 'sync'
    class Runtime:
        def execute(self, command, timeout):
            commands.append(command)
            if command.startswith('cat ') and command.endswith('.exit'):
                return CommandResult(0, '0', '', .1)
            if command.startswith('cat ') and command.endswith('agent.stdout'):
                return CommandResult(0, '{"result":"root returned"}', '', .1)
            if command.startswith('cat ') and command.endswith('agent.stderr'):
                return CommandResult(0, 'EMBEDDED FALLBACK:' if fallback else '', '', .1)
            if command.startswith('cat ') and command.endswith('subagents.stdout'):
                return CommandResult(0, '{"all_sessions_idle":true,"native_cleanup_complete":true}', '', .1)
            if ' agent ' in command:
                return CommandResult(0, '{"result":"root returned"}', 'EMBEDDED FALLBACK:' if fallback else '', .1)
            if 'subagent_drain' in command:
                return CommandResult(0, '{"all_sessions_idle":true,"native_cleanup_complete":true}', '', .1)
            return CommandResult(0, '', '', .1)
    monkeypatch.setenv('OPENCLAW_API_KEY', 'secret')
    runtime = Runtime()
    def run():
        return run_openclaw(prompt='delegate', session_id='root',
        configuration={'base_url': 'http://model.test/v1', 'model': 'model', 'host_live': True,
                       'subagents': True, 'kb_http_timeout_seconds': 30},
        ssh=NativeSSHConfig('executor@tool:2222', 'private', 'ssh-ed25519 public', sandbox_id='tool'),
            policy_control=PolicySession(), runtime_executor=runtime, output_dir=tmp_path, timeout_seconds=60,
            resident_poll=runtime.execute if detached else None,
            resident_file_nonempty=(lambda _path, _timeout: True) if detached else None,
            cancelled=lambda: execution_mode == 'cancel_children' and any('subagent_drain' in c for c in commands))
    if fallback:
        with pytest.raises(RuntimeError, match='fell back to embedded'):
            run()
        assert not any('subagent_drain' in c for c in commands)
        return
    if execution_mode == 'cancel_children':
        with pytest.raises(InterruptedError, match='shared sessions cancelled'):
            run()
        assert not any('tar -cPzhf' in c for c in commands)
        return
    outcome = run()
    assert any('gateway run' in c for c in commands)
    assert any('gateway health' in c for c in commands)
    agent = next(c for c in commands if ' agent ' in c)
    assert '--local' not in agent
    drain = next(i for i, c in enumerate(commands) if 'subagent_drain' in c)
    cleanup = next(i for i, c in enumerate(commands) if 'tar -cPzhf' in c)
    assert drain < cleanup
    assert json.loads(outcome['stdout'])['initial_root_response']['result'] == 'root returned'
    assert outcome['subagents_enabled']


def test_drain_observes_cleanup_after_sessions_are_already_idle(monkeypatch, tmp_path, capsys):
    from contextlib import nullcontext
    from io import BytesIO
    from clawbox.experiments import subagent_drain as module
    registry = tmp_path / 'subagents' / 'runs.json'
    registry.parent.mkdir()
    registry.write_text(json.dumps({'runs': {'child': {'endedAt': 1}}}))
    snapshots = [{'identity': {'session_id': 'root'}, 'payload': {'phase': 'IDLE', 'state_revision': 1}}]
    clock = [0.]
    def advance(seconds):
        clock[0] += seconds
        if clock[0] >= 1:
            registry.write_text(json.dumps({'runs': {'child': {'endedAt': 1, 'cleanupCompletedAt': 2}}}))
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', advance)
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *_a, **_kw:
        nullcontext(BytesIO(json.dumps({'sessions': snapshots}).encode())))
    monkeypatch.setattr('sys.argv', ['subagent_drain', '--state-dir', str(tmp_path),
                                   '--root-session-id', 'root', '--timeout', '10'])
    module.main()
    assert json.loads(capsys.readouterr().out)['native_cleanup_complete']


def test_pressure_pause_records_all_waiting_request_ids():
    import ast
    from pathlib import Path
    source = Path(__file__).parents[1] / 'clawbox/experiments/worker.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    callback = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == 'record_pressure_pause')
    records = []
    env = dict(Any=object, events=SimpleNamespace(write=records.append), peer_enabled=False)
    exec(compile(ast.Module(body=[callback], type_ignores=[]), str(source), 'exec'), env)
    vm = SimpleNamespace(timings=[], tier=SimpleNamespace(value='warm'))
    state = SimpleNamespace(session_id='pair', lifecycle=vm, runtime_lifecycle=vm,
                            model_waits={'root': object(), 'child': object()})
    env['record_pressure_pause'](state, .1, None, 'pressure')
    assert records[0]['request_id'] is None
    assert records[0]['request_ids'] == ['root', 'child']
