"""Run the ordinary worker's resume callback with controlled native boundaries."""
import ast
import time
from contextlib import ExitStack
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest

from clawbox.experiments.session_binding import choose_resume
from test_session_binding import pair


class Settings(SimpleNamespace):
    def model_copy(self, *, update):
        return Settings(**(vars(self) | update))


def callback_environment(tmp_path, *, failure=None):
    scheduler, ledger, events, binding, source = pair()
    calls = []
    resources = Settings(
        lifecycle_reservation_mib=lambda amount: amount,
        snapshot_scheduling_storage=Settings(
            local_roots={'n0': str(tmp_path), 'n1': str(tmp_path)},
            restore_costs=Settings(provenance='calibration', resource_wait_seconds=10.,
                native_restore_seconds=.1, effective_transfer_mib_per_second=100., import_seconds=.01)),
        local_numa_node=0, cold_snapshot_root=str(tmp_path / 'n0'))
    arm = Settings(resources=resources, runtime=Settings(memory_mib=1),
        execution=Settings(resume_policy='cost-aware', migration_threshold_seconds=.25, arm_timeout_seconds=1))
    for i, domain in enumerate(scheduler.domains.values()):
        domain.arm = arm.model_copy(update={'resources': resources.model_copy(update={
            'local_numa_node': i, 'cold_snapshot_root': str(tmp_path / f'n{i}')})})
        domain.borrower = None
        domain.controller = None
    for role, vm in binding.lifecycles.items():
        vm.snapshot_pool = None
        vm._snapshot_key = None
        vm._generation = 1
        vm.sandbox_id = role
        def export(role=role):
            calls.append(('export', role))
            return {'role': role, 'objects': [{'object_id': role}]}
        def bind(ready, role=role):
            assert [item for item in calls if item[0] == 'stage'] == [('stage', 'runtime'), ('stage', 'tool')]
            calls.append(('bind', role))
            return {'import_elapsed_seconds': .1, 'import_phase_seconds': {'shared_decompress_s': .03,
                'target_decompress_s': .02, 'native_import_s': .04, 'catalog_lookup_s': .005,
                'metadata_alias_rewrite_s': .005}}
        def restore(role=role, vm=vm):
            calls.append(('restore', role))
            if failure == 'native':
                vm._state = 'unknown'
                raise ConnectionError('native result unknown')
            vm._state = 'running'
            vm.resident = True
            return .1
        vm.export_paused_generation = export
        vm.bind_replica = bind
        vm.restore = restore
    scheduler.suspend_pair('session', tool=binding.lifecycles['tool'], runtime=binding.lifecycles['runtime'])
    scheduler.sessions['other'] = ('n0', 2, 'another-template')
    def stage(manifest, target):
        calls.append(('stage', manifest['role']))
        if failure == 'transfer':
            raise ConnectionError('transfer failed')
        return {'storage_domain': target}
    environment = dict(
        arm=arm, coordinator=source, watermark_controller=None, numa_borrower=None,
        session_binding=binding, session_placement=scheduler, session_id='session',
        lifecycle=binding.lifecycles['tool'], runtime_lifecycle=binding.lifecycles['runtime'],
        reservation_lock=Lock(), pending_admissions=set(), active_reservations={},
        events=SimpleNamespace(write=events.append), index=0, priority_domain=None,
        peer_cpu=None,
        current_memory_samples=lambda: {}, ExitStack=ExitStack, choose_resume=choose_resume,
        time=time,
        shutil=SimpleNamespace(disk_usage=lambda root: SimpleNamespace(total=100*1024**3, used=0)),
        peer_store=SimpleNamespace(
            estimate_missing=lambda manifest, node: {'payload_bytes': 1024**2},
            active_transfers=lambda source, target: 1, stage_replica=stage),
    )
    path = Path(__file__).resolve().parents[1] / 'clawbox/experiments/worker.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    callback = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == 'resume_peer_pair')
    # Execute the exact callback body while exposing its captured cells for
    # assertions; the worker supplies these cells from _run_session in use.
    callback.body = [ast.Global(names=node.names) if isinstance(node, ast.Nonlocal) else node
                     for node in callback.body]
    module = ast.fix_missing_locations(ast.Module(body=[callback], type_ignores=[]))
    exec(compile(module, str(path), 'exec'), environment)
    return environment, scheduler, ledger, events, binding, calls


def test_worker_stages_both_roles_then_restores_runtime_and_moves_authority(tmp_path):
    env, scheduler, ledger, events, binding, calls = callback_environment(tmp_path)
    original = env['coordinator']._sessions['session']
    result = env['resume_peer_pair']('runtime')
    assert result['elapsed'] == .1
    assert binding.current == 'n1' and binding.epoch == 1
    assert env['coordinator'] is scheduler.domains['n1'].coordinator
    assert env['coordinator']._sessions['session'] is original
    assert original.wait_id == 'request'
    assert binding.lifecycles['tool']._state == 'swapped'
    assert calls == [('export', 'runtime'), ('export', 'tool'),
                     ('stage', 'runtime'), ('stage', 'tool'),
                     ('bind', 'runtime'), ('bind', 'tool'), ('restore', 'runtime')]
    assert not ledger.claims
    # Tool restore uses the committed destination and the same CPU pair lease.
    assert env['resume_peer_pair']('tool') is None
    assert len(calls) == 7
    assert scheduler.sessions['session'][0] == 'n1'
    assert any(event['event'] == 'session_migration_committed' for event in events)
    imports = [event for event in events if event['event'] == 'peer_native_import_completed']
    assert len(imports) == 2 and imports[0]['import_elapsed_seconds'] == .1
    assert imports[0]['import_phase_seconds']['shared_decompress_s'] == .03


@pytest.mark.parametrize('failure,transition', [('transfer', 'stable'), ('native', 'unknown')])
def test_worker_failed_migration_never_resumes_source_implicitly(tmp_path, failure, transition):
    env, _, ledger, _, binding, calls = callback_environment(tmp_path, failure=failure)
    with pytest.raises(ConnectionError):
        env['resume_peer_pair']('runtime')
    assert binding.current == 'n0'
    assert binding.transition == transition
    assert not ledger.claims
    assert [call for call in calls if call[0] == 'restore'] == (
        [('restore', 'runtime')] if failure == 'native' else [])
    if failure == 'native':
        with pytest.raises(RuntimeError, match='unknown'):
            env['resume_peer_pair']('runtime')


def test_worker_pending_tool_request_keeps_pair_home(tmp_path):
    env, scheduler, _, events, binding, calls = callback_environment(tmp_path)
    scheduler.release('other')
    env['pending_admissions'].add('tool-request')
    assert env['resume_peer_pair']('runtime') is None
    assert binding.current == 'n0' and not calls
    assert scheduler.sessions['session'][0] == 'n0'
    assert any(event.get('reason') == 'pending_tool_admission' for event in events)
