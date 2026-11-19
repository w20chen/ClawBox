"""B duration consumers must not require or fabricate memory estimates."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawbox.experiments.prediction import (
    PredictionUnavailable, RuntimePredictionProvider, command_sha256,
    prediction_provider_type, prediction_requires_memory, resolve_command_prediction,
)
from clawbox.experiments.priority import tool_duration_prediction
from clawbox.experiments.spec_types import AdmissionPolicy
from clawbox.experiments.worker import ExperimentWorker


COMMAND = 'sleep 5'
DIGEST = command_sha256(COMMAND)


def models(*, memory=False, duration=True):
    targets = {}
    if duration:
        targets['duration_ms'] = dict(status='available', backend='runtime', p50=5000,
            unit='ms', metric_definition='retained_workload_elapsed')
    if memory:
        targets['memory_extra_peak_bytes'] = dict(status='available', backend='runtime',
            p50=2**20, unit='bytes', metric_definition='environment_memory_peak_minus_baseline')
    return {'tool': dict(schema_version='call_load.v2', scope='tool_call',
        memory_measurement='guest_memtotal_minus_memavailable', targets=targets)}


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    from clawbox.experiments import prediction
    monkeypatch.setattr(prediction, '_IMMUTABLE_PREDICTION_CACHE', {})
    from clawtune_kb import FILES, create_seed
    from tool_time.lattice_kb import LatticeTimeKB
    from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB
    lattice, tool = LatticeTimeKB().to_json_obj(), RuntimeToolResourceKB().to_json_obj()
    seed = tmp_path / 'seed'
    create_seed(seed, dict(zip(FILES, (
        ClauseResourceKB().to_json_obj(), tool, lattice,
    ))), provenance={'kind': 'unit-test'})
    path = tmp_path / 'prediction.json'
    kb = json.dumps({'lattice': lattice, 'tool': tool}).encode()
    path.with_suffix('.kb.json').write_bytes(kb)
    path.write_text(json.dumps(dict(schema='clawbox_prediction_v2', repository='repo',
        training_validated=True, kb_sha256=hashlib.sha256(kb).hexdigest(),
        host_calibration=dict(target='host_cgroup_memory_current_increment',
            sample_count=2, scale=1, offset_bytes=0),
        commands=[dict(command=COMMAND, models=models())])))
    return path, seed


def arm_for(mode, admission):
    return SimpleNamespace(policy=SimpleNamespace(admission=AdmissionPolicy(admission)),
        execution=SimpleNamespace(tool_call_cpu_policy='b'),
        resources=SimpleNamespace(prediction_mode=mode, static_tool_memory_mib=128,
            prediction_reservation_scale=1))


@pytest.mark.parametrize('mode', ['frozen', 'runtime', 'cold-start', 'host-live'])
@pytest.mark.parametrize('admission', ['off', 'fixed'])
@pytest.mark.parametrize('duration', [True, False])
def test_b_real_providers_accept_duration_without_memory(
        artifacts, monkeypatch, mode, admission, duration):
    path, seed = artifacts
    native = models(duration=duration)
    monkeypatch.setattr(RuntimePredictionProvider, '_prediction_models', lambda *args: native)
    if mode == 'frozen':
        payload = json.loads(path.read_text())
        payload['commands'][0]['models'] = native
        path.write_text(json.dumps(payload))
    arm = arm_for(mode, admission)
    provider = prediction_provider_type(mode)(
        seed if mode in {'cold-start', 'host-live'} else path,
        repository='repo', require_memory=prediction_requires_memory(arm))
    metadata = {'raw_command_sha256': DIGEST}
    if mode == 'host-live':
        from clawbox.experiments.knowledge.processes import AdmissionMetadata
        project = AdmissionMetadata(str(seed), {}, require_memory=False)
        metadata = project('repo', native, COMMAND, DIGEST)
    request = dict(command=COMMAND, command_sha256=DIGEST, prediction=metadata)
    result = resolve_command_prediction(arm, request, provider)
    assert result['models'] == native
    assert result['predicted_incremental_memory_mib'] is None
    assert tool_duration_prediction(result)['predicted_duration_seconds'] == (5 if duration else None)
    assert ExperimentWorker._tool_reservation_mib(None, arm, prediction=result) == (
        0 if admission == 'off' else 128)
    assert provider.provenance([result])['observed_fallback_rate'] is None
    if mode in {'cold-start', 'host-live'}:
        assert provider.provenance([result])['prediction_scope'] == 'clawtune_cold_start'
    # The same inputs must never bypass predicted memory admission.
    predicted = arm_for(mode, 'predicted')
    with pytest.raises(PredictionUnavailable):
        ExperimentWorker._tool_reservation_mib(None, predicted, prediction=result)
    with pytest.raises(PredictionUnavailable):
        required = prediction_provider_type(mode)(
            seed if mode in {'cold-start', 'host-live'} else path,
            repository='repo', require_memory=prediction_requires_memory(predicted))
        if mode == 'host-live':
            required._metadata({'models': native}, COMMAND, DIGEST)
        else:
            required.resolve(COMMAND, {'raw_command_sha256': DIGEST})


@pytest.mark.parametrize('mode', ['runtime', 'cold-start'])
@pytest.mark.parametrize('first_required', [False, True])
def test_duration_and_memory_projections_do_not_share_cached_results(
        artifacts, monkeypatch, mode, first_required):
    path, seed = artifacts
    native = models(memory=True)
    if mode == 'cold-start':
        native['tool']['memory_measurement'] = 'cgroup_v2_environment_union_v1'
    monkeypatch.setattr(RuntimePredictionProvider, '_prediction_models', lambda *args: native)
    for required in (first_required, not first_required, first_required):
        provider = prediction_provider_type(mode)(seed if mode == 'cold-start' else path,
            repository='repo', require_memory=required)
        result = provider.resolve(COMMAND, {'raw_command_sha256': DIGEST})
        assert result['predicted_incremental_memory_mib'] == (1 if required else None)
        assert tool_duration_prediction(result)['predicted_duration_seconds'] == 5
        with pytest.raises(PredictionUnavailable, match='identity mismatch'):
            provider.resolve(COMMAND, {'raw_command_sha256': 'wrong'})


def test_duration_only_frozen_provider_retains_identity_and_provenance_checks(artifacts):
    path, _ = artifacts
    provider = prediction_provider_type('frozen')(path, repository='repo', require_memory=False)
    with pytest.raises(PredictionUnavailable, match='identity mismatch'):
        provider.resolve(COMMAND, {'raw_command_sha256': 'wrong'})
    with pytest.raises(PredictionUnavailable, match='no command-specific'):
        provider.resolve('unknown', {'raw_command_sha256': command_sha256('unknown')})
    with pytest.raises(ValueError, match='repository mismatch'):
        prediction_provider_type('frozen')(path, repository='other', require_memory=False)
    payload = json.loads(path.read_text())
    payload['training_validated'] = False
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='validated Cube'):
        prediction_provider_type('frozen')(path, repository='repo', require_memory=False)


@pytest.mark.parametrize('admission', ['off', 'fixed'])
def test_b_input_validation_and_gpu_preparation_use_the_same_consumers(
        artifacts, tmp_path, monkeypatch, admission):
    from test_serving_sim_inputs import prepared_source
    from clawbox.experiments.inputs import validate_inputs, validate_launch_inputs
    from clawbox.experiments.serving_sim_inputs import prepare_serving_sim, serving_sim_input_identity
    from clawbox.experiments.spec_types import InferenceBackend
    path, _ = artifacts
    _, _, spec = prepared_source(tmp_path)
    case = spec.workload.cases[0].model_copy(update={'repository': 'repo'})
    identity = {k: getattr(case.sandbox or spec.sandbox, k) for k in
                ('template_id', 'image_digest', 'vcpu', 'memory_mib', 'architecture')}
    payload = json.loads(path.read_text())
    payload.update(sandbox_identity=identity)
    payload.pop('host_calibration')
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(RuntimePredictionProvider, '_prediction_models', lambda *args: models())
    spec = spec.model_copy(update={
        'workload': spec.workload.model_copy(update={'cases': (case,)}),
        'execution': spec.execution.model_copy(update={'tool_call_cpu_policy': 'b'}),
        'resources': spec.resources.model_copy(update={
            'prediction_mode': 'runtime', 'prediction_artifact': str(path)}),
        'policies': (spec.policies[0].model_copy(update={'admission': AdmissionPolicy(admission)}),),
    })
    for validate in (validate_inputs, validate_launch_inputs):
        assert str(path) in validate(spec)['prediction_files']
    report = prepare_serving_sim(spec, tmp_path / 'bundle')
    manifest = json.loads(Path(report['manifest']).read_text())
    receipt = manifest['cases'][0]['prediction_validation']
    assert receipt['identity']['require_memory'] is False
    spec = spec.model_copy(update={'inference': spec.inference.model_copy(update={
        'backend': InferenceBackend.SERVING_SIM,
        'configuration': dict(model='sim', base_url='http://localhost:8000',
                              serving_sim_catalog_manifest=report['manifest'])})})
    assert serving_sim_input_identity(spec)
    predicted = spec.model_copy(update={'policies': (
        spec.policies[0].model_copy(update={'admission': AdmissionPolicy.PREDICTED}),)})
    with pytest.raises(ValueError, match='prediction inputs changed'):
        serving_sim_input_identity(predicted)
    # An edited artifact invalidates the prepared B receipt as well.
    path.write_text(path.read_text() + '\n')
    with pytest.raises(ValueError, match='prediction inputs changed'):
        serving_sim_input_identity(spec)
