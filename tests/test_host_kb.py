from dataclasses import asdict
import json

import pytest

from clawbox.experiments.host_kb import HostKnowledgeManager
from clawbox.experiments.knowledge.protocol import PROTOCOL, command_digest
from clawtune_kb.store import create_seed
from tool_resource.runtime_kb import CompletedCall, ClauseObservation, RuntimeToolResourceKB, ClauseResourceKB, training_target_provenance
from tool_time.lattice_kb import LatticeTimeKB


def setup_manager(tmp_path):
    call = CompletedCall('repo', 'exec', 'sleep 1', 1., 2.)
    row = ClauseObservation('repo', 'sleep', ('sleep', '1'), 1., 2., latency_ms=1000.)
    seed = tmp_path / 'seed'
    create_seed(seed, {'runtime-tool-resource-kb.json': RuntimeToolResourceKB.fit_public([call]).to_json_obj(),
        'clause-resource-kb.json': ClauseResourceKB.fit_public([row]).to_json_obj(),
        'clause-lattice-time-kb.json': LatticeTimeKB.fit([row]).to_json_obj()}, provenance={})
    def make():
        return HostKnowledgeManager(seed, tmp_path / 'kb', run_id='run', attempt_id='attempt', arm_id='arm',
            clawtune_revision='test', metadata=lambda repo, models, command, digest: {'models': models})
    return make


def prediction_payload(session='a'):
    return dict(schema_version=PROTOCOL, session_id=session, event_id='event', tool_call_id='call',
        command='sleep 1', command_sha256=command_digest('sleep 1'), repository='repo', tool_name='exec',
        memory_measurement='cgroup_v2_environment_union_v1', command_context={}, raw_params={'command': 'sleep 1'})


@pytest.mark.parametrize('source', ['tool', 'lattice'])
def test_llc_duration_uses_selected_canonical_host_prediction_receipt(tmp_path, clawtune_shell_parser, source):
    from clawbox.experiments.priority import tool_duration_prediction
    manager = setup_manager(tmp_path)()
    manager.reserve('a', 'repo')
    request = prediction_payload()
    predicted = manager.predict('a', request)
    canonical = manager.claim('a', dict(execution_id='exec',
        prediction_receipt=predicted['prediction_receipt'],
        command=request['command'], command_sha256=request['command_sha256']))
    target = canonical['models'][source]['targets']['duration_ms']
    assert target['status'] == 'available'
    duration = tool_duration_prediction(canonical, source=source)
    assert duration['duration_prediction_model'] == source
    assert duration['duration_prediction_backend'] == target['backend']
    assert duration['predicted_duration_seconds'] == target['p50'] / 1000


def test_durable_learning_lost_ack_and_isolation(tmp_path):
    make = setup_manager(tmp_path)
    manager = make()
    for session in ('a', 'b'):
        manager.reserve(session, 'repo')
    request = prediction_payload()
    predicted = manager.predict('a', request)
    assert manager.predict('a', request) == predicted
    receipt = predicted['prediction_receipt']
    claim = dict(execution_id='exec', prediction_receipt=receipt, command=request['command'], command_sha256=request['command_sha256'])
    manager.claim('a', claim)
    assert manager.claim('a', claim)
    with pytest.raises(ValueError):
        manager.claim('a', {**claim, 'execution_id': 'other'})
    call = CompletedCall('repo', 'exec', 'sleep 1', 1., 4.)
    observation = {key: value for key, value in request.items() if key not in ('event_id', 'tool_call_id', 'command_context', 'raw_params')}
    observation.update(execution_id='exec', prediction_receipt=receipt, call=asdict(call), clauses=[],
        call_provenance=training_target_provenance(call),
        quality=dict(collection_validity='valid', cleanup='ok', loss_total=0, reason=None))
    assert manager.observe('a', observation)['scope_generation'] == 1
    assert manager.observe('a', observation)['status'] == 'duplicate'
    assert manager.scopes['b'].view.generation == 0
    manager.mark_degraded('a', 'later', 'transport failure')
    restarted = make()
    restarted.reserve('a', 'repo')
    assert restarted.observe('a', observation)['status'] == 'duplicate'
    assert restarted.scopes['a'].view.generation == 1
    assert restarted.scopes['a'].degraded
    changed = {**observation, 'call': {**observation['call'], 'ts_end': 5.}}
    with pytest.raises(ValueError, match='different learning payload'):
        restarted.observe('a', changed)
    assert receipt not in next((tmp_path / 'kb/scopes').glob('*/journal.jsonl')).read_text()


def test_session_summary_does_not_wait_for_another_sessions_update(tmp_path):
    from threading import Event, Thread
    manager = setup_manager(tmp_path)()
    manager.reserve('a', 'repo')
    manager.reserve('b', 'repo')
    entered, release = Event(), Event()

    def updating_other_scope():
        with manager.scopes['b'].lock:
            entered.set()
            release.wait(3)

    thread = Thread(target=updating_other_scope)
    thread.start()
    assert entered.wait(2)
    try:
        summary = manager.scope_summary('a')
        assert not release.is_set()
        assert thread.is_alive(), 'summary waited for the other scope to finish'
        assert summary['learning_valid']
    finally:
        release.set()
        thread.join(3)
    assert summary == manager.summary()['scopes']['a']


def test_real_guest_memory_domain_is_used_after_learning_and_recovery(tmp_path):
    make = setup_manager(tmp_path)
    manager = make()
    for session in ('a', 'b'):
        manager.reserve(session, 'repo')
    request = prediction_payload('a')
    initial = manager.predict('a', request)
    before_b = manager.predict('b', prediction_payload('b'))
    assert initial['prediction']['tool'] == before_b['prediction']['tool']
    receipt = initial['prediction_receipt']
    manager.claim('a', dict(execution_id='memory-exec', prediction_receipt=receipt,
        command=request['command'], command_sha256=request['command_sha256']))
    call = CompletedCall('repo', 'exec', 'sleep 1', 1., 4.,
        memory_measurement='guest_memtotal_minus_memavailable', memory_eligible=True,
        memory_baseline_bytes=64 * 1024**2, memory_total_peak_bytes=192 * 1024**2,
        memory_extra_peak_bytes=128 * 1024**2, memory_environment_id='guest-a')
    observation = {key: value for key, value in request.items()
        if key not in ('event_id', 'tool_call_id', 'command_context', 'raw_params')}
    observation.update(execution_id='memory-exec', prediction_receipt=receipt,
        call=asdict(call), clauses=[], call_provenance=training_target_provenance(call),
        quality=dict(collection_validity='valid', cleanup='ok', loss_total=0, reason=None))
    manager.observe('a', observation)
    after = manager.predict('a', {**request, 'tool_call_id': 'after'})['prediction']['tool']
    assert after['memory_measurement'] == 'guest_memtotal_minus_memavailable'
    assert after['targets']['memory_extra_peak_bytes']['p50'] == 128 * 1024**2
    after_b = manager.predict('b', {**prediction_payload('b'), 'tool_call_id': 'after'})
    assert after_b['prediction']['tool'] == before_b['prediction']['tool']
    recovered = make()
    recovered.reserve('a', 'repo')
    assert recovered.predict('a', {**request, 'tool_call_id': 'recovered'})['prediction']['tool'] == after


def test_validation_failure_cannot_publish_or_learn(tmp_path, monkeypatch):
    manager = setup_manager(tmp_path)()
    manager.reserve('a', 'repo')
    with pytest.raises(ValueError):
        manager.predict('a', prediction_payload('b'))
    with pytest.raises(ValueError):
        manager.predict('a', {**prediction_payload(), 'command': 'sleep 2'})
    assert manager.scopes['a'].view.generation == 0


def test_failed_journal_does_not_publish_prepared_generation(tmp_path, monkeypatch):
    from copy import deepcopy
    manager = setup_manager(tmp_path)()
    manager.reserve('a', 'repo')
    request = prediction_payload()
    receipt = manager.predict('a', request)['prediction_receipt']
    manager.claim('a', dict(execution_id='exec', prediction_receipt=receipt,
                           command=request['command'], command_sha256=request['command_sha256']))
    call = CompletedCall('repo', 'exec', 'sleep 1', 1., 4.)
    observation = {k:v for k,v in request.items()
                   if k not in ('event_id', 'tool_call_id', 'command_context', 'raw_params')}
    observation.update(execution_id='exec', prediction_receipt=receipt, call=asdict(call), clauses=[],
        call_provenance=training_target_provenance(call),
        quality=dict(collection_validity='valid', cleanup='ok', loss_total=0, reason=None))
    old = manager.scopes['a'].view
    append = manager._append
    def fail(*args):
        raise OSError('injected durable write failure')
    monkeypatch.setattr(manager, '_append', fail)
    with pytest.raises(OSError, match='durable write'):
        manager.observe('a', deepcopy(observation))
    assert manager.scopes['a'].view is old
    assert 'exec' not in manager.scopes['a'].committed
    monkeypatch.setattr(manager, '_append', append)
    assert manager.observe('a', observation)['scope_generation'] == 1


def test_remote_sidecar_does_not_create_local_knowledge(tmp_path):
    from clawbox.experiments.knowledge.runtime_state import build_state
    from clawbox.experiments.knowledge.runtime_config import HostSidecarConfig as SidecarConfig
    from clawbox.experiments.knowledge.remote import RemoteKnowledgeClient
    state = build_state(SidecarConfig(host_kb_endpoint='http://127.0.0.1:1', host_kb_token='test',
        host_kb_session_id='a', tool_resource_artifact_dir=tmp_path / 'kb', trace_dir=tmp_path / 'traces',
        tool_resource_ebpf_enabled=False, pmu_enabled=False))
    try:
        assert isinstance(state.predictor, RemoteKnowledgeClient)
        assert not (tmp_path / 'kb').exists()
        assert not hasattr(state.predictor, 'kb')
    finally:
        state.predictor.close()
        state.trace_writer.close()


def test_runtime_client_uses_authenticated_host_prediction_and_rejects_missing_receipt(tmp_path):
    import httpx
    from types import SimpleNamespace
    from clawbox.experiments.policy_control import PolicyControlServer
    from clawbox.experiments.knowledge.remote import RemoteKnowledgeClient
    manager = setup_manager(tmp_path)()
    manager.reserve('a', 'repo')
    with PolicyControlServer(advertise_host='127.0.0.1', advertised_port=0,
                             bind_host='127.0.0.1', bind_port=0) as server:
        server.knowledge_manager = manager
        session = server.register('a', admit=lambda request: {'decision': 'ADMIT'},
                                  complete=lambda request: {'status': 'COMPLETED'})
        client = RemoteKnowledgeClient(SimpleNamespace(host_kb_endpoint=session.url,
            host_kb_token=session.token, host_kb_session_id='a', host_kb_timeout_seconds=3., tool_resource_repo='repo'))
        request = SimpleNamespace(tool_name='exec', event_id='event', tool_call_id='call',
            raw_params={'command': (
                'CLAWBOX_DEFERRED_TOOL_TIMEOUT_SECONDS=1200\n'
                'sleep 1'
            )}, command_context={})
        try:
            prediction = client.predict(request)
            receipt = prediction.tool_resource['host_kb']['prediction_receipt']
            body = dict(session_id='a', execution_id='exec', command='sleep 1', command_sha256=command_digest('sleep 1'))
            assert client.client.post('/v1/tool/admit', json=body).status_code == 400
            body['prediction_receipt'] = receipt
            assert client.client.post('/v1/tool/admit', json=body).status_code == 200
            assert client.client.post('/v1/tool/complete', json=body).status_code == 200
        finally:
            client.close()
            session.discard()
