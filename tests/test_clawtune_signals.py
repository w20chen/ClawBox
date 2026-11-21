"""Exercise the delivered host adapter through current ClawTune HTTP contracts."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from test_host_kb import setup_manager, prediction_payload
from clawbox.experiments.knowledge.runtime_config import HostSidecarConfig
from clawbox.experiments.knowledge.runtime_state import build_state
from clawbox.experiments.policy_control import PolicyControlServer
from clawbox.experiments.spec import ClawTuneSignalsSpec
from clawtune_kb.contracts import data_root, validate
from clawtune_sidecar.api.app import create_app


def example(name):
    return json.loads((data_root() / 'contracts/examples' / name).read_text(encoding='utf-8'))


@pytest.fixture
def signal_receiver():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread
    received, delivered = [], Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.headers.get('Authorization'), body))
            self.send_response(200)
            self.end_headers()
            delivered.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/signals', received, delivered
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_host_sidecar_preview_decision_completion_and_session(tmp_path, signal_receiver):
    endpoint, received, delivered = signal_receiver
    manager = setup_manager(tmp_path)()
    manager.reserve('a', 'repo')
    with PolicyControlServer(advertise_host='127.0.0.1', advertised_port=0,
                             bind_host='127.0.0.1', bind_port=0) as server:
        server.knowledge_manager = manager
        protected = []
        session = server.register('a', admit=lambda r: {'decision': 'ADMIT'},
                                  complete=lambda r: {'status': 'COMPLETED'},
                                  shared_runtime=lambda: protected.append(True))
        state = build_state(HostSidecarConfig(
            trace_dir=tmp_path / 'traces', auth_token='test-token',
            signal_endpoint=endpoint, signal_token='receiver-only',
            host_kb_endpoint=session.url, host_kb_token=session.token, host_kb_session_id='a',
            tool_resource_repo='repo', tool_resource_ebpf_enabled=False, pmu_enabled=False))
        app = create_app(state)
        from clawbox.experiments.knowledge.session_forecast import install_host_session_forecasts
        install_host_session_forecasts(app, state)
        request = example('tool-before-request.json')
        request.update(raw_params={'command': 'sleep 1'}, repo='repo', session_id='a', run_id='run')
        headers = {'Authorization': 'Bearer test-token'}
        with TestClient(app) as client:
            register = '/v1/clawbox/session-register'
            assert client.post(register, json={'session_key': 'root'}).status_code == 401
            assert client.post(register, json={'session_key': 'root'}, headers=headers).json()['shared_runtime'] is False
            assert client.post(register, json={'session_key': 'child'}, headers=headers).json()['shared_runtime'] is True
            assert protected == [True]
            assert client.post('/v1/predictions/tool', json=request).status_code == 401
            for _ in range(2):
                preview = client.post('/v1/predictions/tool', json=request, headers=headers)
                assert preview.status_code == 200, preview.text
                validate(preview.json(), 'call-load.schema.json')
            scope = manager.scopes['a']
            assert not scope.receipts and not scope.predictions and not scope.features._calls
            assert not app.state.signals.recent
            response = client.post('/v1/decisions/tool', json=request, headers=headers)
            assert response.status_code == 200, response.text
            decision = response.json()
            validate(decision, 'tool-decision.schema.json')
            receipt = decision['placement']['host_kb']['prediction_receipt']
            assert receipt
            assert len(scope.receipts) == 1
            assert client.post('/v1/decisions/tool', json=request, headers=headers).json() == decision
            signal = app.state.signals.recent[-1]
            validate(signal, 'signal.schema.json')
            assert signal['type'] == 'prediction.created'
            assert signal['payload']['prediction'] == decision['prediction']
            assert receipt not in json.dumps(signal)
            assert delivered.wait(3), 'prediction publication was not received'
            assert received[0][0] == 'Bearer receiver-only'
            assert received[0][1] == signal
            assert not state.predictor.receipts
            from clawtune_sidecar.signal_protocol import event_signal
            from clawtune_sidecar.contracts.models import ToolCompletedEvent
            completion = ToolCompletedEvent(**{
                key: request.get(key) for key in ('schema_version', 'plugin_version', 'run_id',
                    'session_id', 'session_key', 'agent_id', 'gateway_id', 'runtime_id', 'tool_call_id', 'tool_name')
            }, event_id='done', occurred_at='2026-11-21T00:00:00Z',
                decision_id=decision['decision_id'], lease_id=None, duration_ms=1000,
                succeeded=True, error_type=None, error_digest=None)
            completed = client.post('/v1/signals', json=event_signal(completion), headers=headers)
            assert completed.status_code == 200, completed.text
            assert app.state.signals.recent[-1]['type'] == 'tool.completed'
            assert scope.view.generation == 0  # only Tool VM observations may train host KB
            # Session observations run through the real upstream state machine.
            message = example('signal-session-observation.json')
            message['observed_at'] = time.time()
            message['payload']['observer_started_at'] = time.time() - 1
            assert client.post('/v1/signals', json=message, headers=headers).status_code == 200
            snapshots = client.get('/v1/sessions', headers=headers).json()['sessions']
            assert snapshots[0]['payload']['phase'] == 'TOOLS'
            message['payload']['revision'] += 1
            message['payload']['batch']['tools'][0]['settled'] = True
            message['observed_at'] = time.time()
            assert client.post('/v1/signals', json=message, headers=headers).status_code == 200
            assert client.get('/v1/sessions', headers=headers).json()['sessions'][0]['payload']['phase'] == 'HANDOFF'
            # Non-shell tools remain valid but explicitly lack host-KB evidence.
            request.update(event_id='native', tool_call_id='native', tool_name='read', raw_params={'path': 'a'})
            response = client.post('/v1/decisions/tool', json=request, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()['prediction']['tool']['load']['targets']['duration_ms']['status'] == 'unavailable'
        session.discard()


def test_session_preview_does_not_bind_later_command_or_advance_learning(tmp_path):
    manager = setup_manager(tmp_path)()
    manager.reserve('a', 'repo')
    request = prediction_payload()
    before = manager.scopes['a'].view.generation
    manager.preview('a', request)
    changed = {**request, 'raw_params': {'command': 'sleep 2'}, 'command': 'sleep 2'}
    from clawbox.experiments.knowledge.protocol import command_digest
    changed['command_sha256'] = command_digest(changed['command'])
    assert manager.predict('a', changed)['prediction_receipt']
    assert manager.scopes['a'].view.generation == before


def test_signal_configuration_passes_only_named_receiver_credential(monkeypatch):
    monkeypatch.setenv('RECEIVER_TOKEN', 'secret')
    settings = ClawTuneSignalsSpec(endpoint='http://192.0.2.1:9000/signals',
                                  token_env='RECEIVER_TOKEN', tool_events=False)
    assert settings.environment()['CLAWTUNE_SIGNAL_TOKEN'] == 'secret'
    assert settings.environment()['CLAWTUNE_SIGNAL_TOOL_EVENTS'] == 'false'
    assert 'secret' not in settings.model_dump_json()
    monkeypatch.delenv('RECEIVER_TOKEN')
    with pytest.raises(ValueError, match='set RECEIVER_TOKEN'):
        settings.environment()
    for endpoint in ('ftp://example.com', 'http://user:password@example.com', 'http://example.com/#a'):
        with pytest.raises(ValueError):
            ClawTuneSignalsSpec(endpoint=endpoint)


def test_signal_settings_survive_public_baseline_and_frozen_run(tmp_path):
    from test_unified_configuration import documents, compile_pair
    from clawbox.experiments.configuration import dump_experiment
    from clawbox.experiments.spec import load_experiment
    baseline, workload = documents()
    settings = dict(endpoint='http://192.0.2.1:9000/signals', token_env='RECEIVER_TOKEN', tool_events=False)
    baseline['execution']['clawtune_signals'] = settings
    spec = compile_pair(baseline, workload)
    path = tmp_path / 'experiment.yaml'
    path.write_text(dump_experiment(spec), encoding='utf-8')
    assert load_experiment(path).execution.clawtune_signals == ClawTuneSignalsSpec(**settings)


def test_native_state_uses_current_predictor_and_monitor(tmp_path):
    state = build_state(HostSidecarConfig(trace_dir=tmp_path, tool_resource_ebpf_enabled=False,
                                         pmu_enabled=False, tool_resource_artifact_dir=tmp_path / 'tool-resource'))
    with TestClient(create_app(state)) as client:
        response = client.post('/v1/decisions/tool', json=example('tool-before-request.json'))
        assert response.status_code == 200, response.text
        validate(response.json(), 'tool-decision.schema.json')


def test_queued_host_admission_reaches_session_eta_over_http(tmp_path):
    import httpx
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from test_admission_forecast import setup_forecaster, train
    from clawbox.experiments.knowledge.session_forecast import install_host_session_forecasts
    from clawtune_sidecar.signal_protocol import envelope
    manager = setup_manager(tmp_path)()
    manager.reserve('s', 'repo')
    forecaster, now, _, conditions = setup_forecaster()
    train(forecaster, now)
    queued, release = Event(), Event()
    with PolicyControlServer(advertise_host='127.0.0.1', advertised_port=0,
                             bind_host='127.0.0.1', bind_port=0) as server:
        server.forecaster = forecaster
        server.knowledge_manager = manager

        def admit(body):
            forecaster.predicted('s', body['execution_id'], duration=10., memory=100, dependency_limited=False)
            queued.set()
            assert release.wait(15)
            return dict(decision='ADMIT', sandbox_id='vm', epoch=1)

        session = server.register('s', admit=admit, complete=lambda r: {'status': 'COMPLETED'})
        state = build_state(HostSidecarConfig(trace_dir=tmp_path / 'traces',
            host_kb_endpoint=session.url, host_kb_token=session.token, host_kb_session_id='s',
            tool_resource_repo='repo', tool_resource_ebpf_enabled=False, pmu_enabled=False))
        app = create_app(state)
        install_host_session_forecasts(app, state)
        request = example('tool-before-request.json')
        request.update(raw_params={'command': 'sleep 1'}, repo='repo', session_id='openclaw-session', run_id='run')
        owner = {k: request[k] for k in ('gateway_id', 'runtime_id', 'session_id', 'run_id')}
        app.state.signals.sessions.handoff_history[(owner['gateway_id'], owner['runtime_id'])].append(100)
        with TestClient(app) as client, httpx.Client(base_url=session.url,
                headers={'Authorization': 'Bearer ' + session.token}, timeout=20, trust_env=False) as host:
            decision = client.post('/v1/decisions/tool', json=request).json()
            receipt = decision['placement']['host_kb']['prediction_receipt']
            body = dict(session_id='s', execution_id='execution', command='sleep 1',
                command_sha256=prediction_payload()['command_sha256'], prediction_receipt=receipt)
            message = envelope('session.observation', dict(revision=1, observer_id='observer',
                observer_started_at=time.time()-1, session_key='key', parent_session_key=None,
                run_status='active', model_call_id=None, batch=dict(batch_id='batch',
                execution_mode='parallel', mode_source='observed_overlap', tools=[dict(
                    tool_call_id=request['tool_call_id'], started=True, settled=False, foreground=True,
                    predicted_duration_ms=1000, prediction_source='lattice')])), source='plugin', identity=owner)
            assert client.post('/v1/signals', json=message).status_code == 200
            with ThreadPoolExecutor(max_workers=1) as pool:
                admission = pool.submit(host.post, '/v1/tool/admit', json=body)
                try:
                    assert queued.wait(5)
                    now[0] += 5
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        value = client.get('/v1/sessions').json()['sessions'][0]
                        if value['payload']['remaining_ms'] is not None:
                            break
                        time.sleep(.05)
                    assert value['payload']['remaining_ms'] == pytest.approx(15100, abs=150)
                    assert not admission.done()
                    assert receipt not in json.dumps(value)
                    validate(value, 'signal.schema.json')
                finally:
                    release.set()
                assert admission.result().status_code == 200
            boundary = {**body, 'endpoint_sandbox_id': 'vm', 'endpoint_epoch': 1, 'phase': 'start'}
            assert host.post('/v1/tool/start', json=boundary).status_code == 200
            now[0] += 2
            status = host.post('/v1/tool/forecast', json={'receipts': [receipt]}).json()
            assert status['tools'][receipt]['remaining_ms'] == pytest.approx(8000)
            conditions[0] += 1
            assert host.post('/v1/tool/forecast', json={'receipts': [receipt]}).json()['tools'][receipt]['remaining_ms'] is None
            assert host.post('/v1/tool/complete', json=body).status_code == 200
        session.discard()
