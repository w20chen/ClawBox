import json
import pytest
from clawbox.experiments.prediction import select_p50, P50PredictionProvider, PredictionUnavailable


def model(backend, value):
    return {'schema_version': 'call_load.v2', 'scope': 'tool_call',
            'memory_measurement': 'guest_memtotal_minus_memavailable',
            'targets': {'memory_extra_peak_bytes': {'backend': backend, 'status': 'available',
              'unit': 'bytes', 'metric_definition': 'environment_memory_peak_minus_baseline',
              'p50': value, 'p90': value*10, 'sample_count': 3}}}


def test_p50_prefers_lattice_even_when_toolkb_is_smaller():
    result = select_p50({'lattice': model('lattice', 2**20), 'tool': model('runtime', 0)})
    assert result['predicted_incremental_memory_mib'] == 1
    assert result['fallback_level'] == 'lattice'
    assert result['prediction_quantile'] == .5


def test_toolkb_fallback_keeps_measurement_contract():
    tool = model('runtime', 2**21)
    assert select_p50({'tool': tool})['predicted_incremental_memory_mib'] == 2
    tool['memory_measurement'] = 'cgroup_v2_memory_current'
    with pytest.raises(PredictionUnavailable):
        select_p50({'tool': tool})


def test_lightweight_requires_measured_short_duration_and_sampling_reason():
    good = {'count': 2, 'max_duration_ms': 9, 'reason': 'no_in_execution_memory_sample'}
    assert select_p50({}, short_call_evidence=good)['fallback_level'] == 'short_call_assumption'
    for change in [{'count': 0}, {'max_duration_ms': 21}, {'reason': 'memory_sampling_gap'}, {'max_duration_ms': float('nan')}]:
        with pytest.raises(PredictionUnavailable):
            select_p50({}, short_call_evidence={**good, **change})


def test_frozen_provider_checks_repository_command_and_uses_training_prediction(tmp_path):
    path = tmp_path/'p50.json'
    path.write_text(json.dumps({'schema': 'clawbox_p50_v1', 'repository': 'r', 'training_validated': True,
        'commands': [{'command': 'pytest', 'models': {'tool': model('runtime', 2**20)}}]}))
    provider = P50PredictionProvider(path, repository='r')
    assert provider.max_incremental_memory_mib == 1
    entry = next(iter(provider.manifest.values()))
    assert provider.resolve('pytest', entry)['fallback_level'] == 'tool'
    assert provider.resolve('pytest', {
        'raw_command_sha256': entry['raw_command_sha256'],
        'call_prediction': model('lattice', 999 * 2**20),
    })['predicted_incremental_memory_mib'] == 1
    assert provider.provenance([
        {'prediction_source': 'frozen_clawbox_p50', 'fallback_level': 'lattice'},
        {'prediction_source': 'frozen_clawbox_p50', 'fallback_level': 'tool'},
        {'prediction_source': 'filesystem_static', 'fallback_level': 'not_applicable'},
    ])['observed_fallback_rate'] == .5
    with pytest.raises(PredictionUnavailable):
        provider.resolve('unseen', entry)
    with pytest.raises(ValueError):
        P50PredictionProvider(path, repository='different')


def test_frozen_provider_rejects_unvalidated_or_different_environment(tmp_path):
    path = tmp_path/'p50.json'
    identity = {'image_digest': 'sha256:training', 'vcpu': 2, 'memory_mib': 4096,
                'architecture': 'arm64'}
    payload = {'schema': 'clawbox_p50_v1', 'repository': 'r',
               'sandbox_identity': identity, 'commands': []}
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='validated Cube'):
        P50PredictionProvider(path, repository='r', sandbox_identity=identity)
    payload['training_validated'] = True
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='environments differ'):
        P50PredictionProvider(path, repository='r',
                              sandbox_identity={**identity, 'memory_mib': 8192})
    P50PredictionProvider(path, repository='r', sandbox_identity=identity)
