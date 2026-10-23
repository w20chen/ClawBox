import json
import pytest
import hashlib
from types import SimpleNamespace
from clawbox.experiments.prediction import (
    select_cold_start_p50, select_p50, P50PredictionProvider,
    PredictionUnavailable,
)


def test_runtime_prediction_queries_new_command_and_checks_kb_identity(tmp_path, monkeypatch):
    from clawbox.clawtune_integration import use_clawtune
    use_clawtune()
    from tool_time.lattice_kb import LatticeTimeKB
    from tool_resource.runtime_kb import RuntimeToolResourceKB
    from clawbox.experiments.prediction import RuntimePredictionProvider, command_sha256
    from clawbox.tuning import clawtune
    path = tmp_path / 'runtime.json'
    kb = json.dumps({'lattice': LatticeTimeKB().to_json_obj(),
                     'tool': RuntimeToolResourceKB().to_json_obj()}).encode()
    path.with_suffix('.kb.json').write_bytes(kb)
    path.write_text(json.dumps({'schema': 'clawbox_prediction_v2', 'repository': 'r',
        'training_validated': True, 'kb_sha256': hashlib.sha256(kb).hexdigest(),
        'host_calibration': {'target': 'host_cgroup_memory_current_increment',
                            'sample_count': 3, 'scale': 2, 'offset_bytes': 2**20}}))
    seen = []
    def predict(runtime, query, **kwargs):
        seen.append(query.command)
        return {key: SimpleNamespace(model_dump=lambda **_: model('runtime', 3 * 2**20))
                if key == 'tool' else SimpleNamespace(model_dump=lambda **_: {})
                for key in ('tool', 'lattice')}
    monkeypatch.setattr(clawtune, 'predict_native_call_load_models', predict)
    provider = RuntimePredictionProvider(path, repository='r')
    command = 'python unseen_task.py'
    result = provider.resolve(command, {'raw_command_sha256': command_sha256(command)})
    assert seen == [command, None]
    assert result['predicted_guest_extra_memory_mib'] == 3
    assert result['predicted_incremental_memory_mib'] == 7
    assert result['prediction_source'] == 'runtime_clawtune_immutable_kb'
    assert not provider.manifest  # No exact-command replay table is needed.
    second = RuntimePredictionProvider(path, repository='r')
    assert second.runtime is provider.runtime
    assert second.lattice is provider.lattice
    assert second.runtime._frozen is True
    assert second.lattice._frozen is True
    assert second.resolve(
        command, {'raw_command_sha256': command_sha256(command)},
    )['predicted_incremental_memory_mib'] == 7
    assert seen == [command, None]  # immutable result was memoized
    with pytest.raises(PredictionUnavailable, match='identity mismatch'):
        provider.resolve(command, {'raw_command_sha256': command_sha256('different')})
    path.with_suffix('.kb.json').write_bytes(kb + b' ')
    with pytest.raises(ValueError, match='KB hash'):
        RuntimePredictionProvider(path, repository='r')


def test_cold_start_prediction_accepts_any_repo_but_requires_same_environment(tmp_path):
    from clawbox.clawtune_integration import use_clawtune
    use_clawtune()
    from clawtune_kb import FILES, create_seed
    from tool_time.lattice_kb import LatticeTimeKB
    from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB
    from clawbox.experiments.prediction import (
        ColdStartPredictionProvider, prediction_input_paths,
    )
    path = tmp_path / 'bootstrap-v2'
    identity = {'template_id': 'training', 'image_digest': 'sha256:image',
                'vcpu': 2, 'memory_mib': 4096, 'architecture': 'arm64'}
    second_identity = {**identity, 'template_id': 'second', 'image_digest': 'sha256:second'}
    create_seed(path, dict(zip(FILES, (
        ClauseResourceKB().to_json_obj(), RuntimeToolResourceKB().to_json_obj(),
        LatticeTimeKB().to_json_obj(),
    ))), provenance={
        'kind': 'clawbox-qualified-observation-import',
        'admission': {
            'sandbox_identities': [identity, second_identity],
            'sandbox_identities_sha256': 'calibrated-environment-set',
            'host_calibrations': {
                'guest': {
                    'target': 'host_cgroup_memory_current_increment',
                    'sample_count': 3, 'scale': 2, 'offset_bytes': 0,
                },
            },
        },
        'admission_prediction_policy': [{
            'model': 'lattice', 'level': 'lattice_guest_extra',
            'target': 'memory_extra_peak_bytes',
            'measurement': 'guest_memtotal_minus_memavailable',
            'semantics': 'exact', 'host_calibration': 'guest',
        }],
    })
    resized = {**identity, 'template_id': 'evaluation', 'memory_mib': 3072}
    provider = ColdStartPredictionProvider(path, repository='another/repo',
                                           sandbox_identity=identity)
    assert provider.repository == 'another/repo'
    assert provider.prediction_source == 'runtime_clawtune_cold_start_kb'
    assert ColdStartPredictionProvider(
        path, repository='third/repo', sandbox_identity=second_identity,
    ).provenance()['calibration_sandbox_identity_count'] == 2
    with pytest.raises(ValueError, match='environments differ'):
        ColdStartPredictionProvider(path, repository='another/repo', sandbox_identity=resized)
    with pytest.raises(ValueError, match='environments differ'):
        ColdStartPredictionProvider(path, repository='another/repo',
                                    sandbox_identity={**resized, 'image_digest': 'sha256:other'})
    assert prediction_input_paths(path, 'cold-start') == [
        path / 'manifest.json',
        *(path / name for name in sorted(FILES)),
    ]


def test_reservation_scale_does_not_change_model_prediction():
    from clawbox.experiments.spec import ExperimentSpec, expand_matrix
    from clawbox.experiments.worker import ExperimentWorker
    from test_experiments_v2 import raw_spec
    raw = raw_spec()
    raw['policies'] = [dict(name='prediction', admission='predicted', runtime_control='off',
                            reclamation='resident', eviction='none', restore='none')]
    raw['resources']['prediction_reservation_scale'] = .5
    arm = expand_matrix(ExperimentSpec.model_validate(raw))[0]
    prediction = {'admission_prediction_target': 'host_cgroup_memory_current_increment',
                  'predicted_incremental_memory_mib': 100.5}
    assert ExperimentWorker._tool_reservation_mib(None, arm, prediction=prediction) == 51
    assert prediction['predicted_incremental_memory_mib'] == 100.5
    for invalid in (0, -1, float('inf'), float('nan')):
        raw['resources']['prediction_reservation_scale'] = invalid
        with pytest.raises(ValueError):
            ExperimentSpec.model_validate(raw)


@pytest.mark.parametrize('mode', ['fixed', 'predicted'])
def test_non_command_budget_is_identical_and_unscaled_across_admission_arms(mode):
    from clawbox.experiments.spec import ExperimentSpec, expand_matrix
    from clawbox.experiments.worker import ExperimentWorker
    from test_experiments_v2 import raw_spec
    raw = raw_spec()
    raw['policies'] = [dict(name=mode, admission=mode, runtime_control='off',
                           reclamation='resident', eviction='none', restore='none')]
    raw['resources'].update(static_tool_memory_mib=630, non_command_tool_memory_mib=128,
                            prediction_reservation_scale=.1)
    arm = expand_matrix(ExperimentSpec.model_validate(raw))[0]
    prediction = dict(prediction_source='non_command_fixed_host_budget',
                      admission_prediction_target='host_cgroup_memory_current_increment',
                      predicted_incremental_memory_mib=128)
    assert ExperimentWorker._tool_reservation_mib(None, arm, prediction=prediction) == 128
    if mode == 'fixed':
        # Managed exec calls do not carry the non-command marker and must use
        # the independently calibrated fixed reservation.
        assert ExperimentWorker._tool_reservation_mib(None, arm, prediction=None) == 630


def model(backend, value):
    return {'schema_version': 'call_load.v2', 'scope': 'tool_call',
            'memory_measurement': 'guest_memtotal_minus_memavailable',
            'targets': {'memory_extra_peak_bytes': {'backend': backend, 'status': 'available',
              'unit': 'bytes', 'metric_definition': 'environment_memory_peak_minus_baseline',
              'p50': value, 'p90': value*10, 'sample_count': 3}}}


def rss_model(backend, value):
    result = model(backend, value)
    result['targets']['memory_extra_peak_bytes'] = {
        'backend': backend, 'status': 'unavailable', 'unit': 'bytes',
        'metric_definition': 'environment_memory_peak_minus_baseline',
        'unavailable_reason': 'missing_clause_evidence',
    }
    result['targets']['sampled_peak_rss_bytes'] = {
        'backend': backend, 'status': 'available', 'unit': 'bytes',
        'metric_definition': 'ebpf_sampled_distinct_mm_rss_peak',
        'p50': value, 'p90': value * 10, 'sample_count': 3,
    }
    return result


def test_p50_prefers_lattice_even_when_toolkb_is_smaller():
    result = select_p50({'lattice': model('lattice', 2**20), 'tool': model('runtime', 0)})
    assert result['predicted_incremental_memory_mib'] == 1
    assert result['fallback_level'] == 'lattice'
    assert result['prediction_quantile'] == .5


def test_cold_start_uses_lattice_rss_proxy_before_exact_tool_memory():
    policy = [
        {'model': 'lattice', 'level': 'lattice_guest_extra',
         'target': 'memory_extra_peak_bytes',
         'measurement': 'guest_memtotal_minus_memavailable',
         'semantics': 'exact', 'host_calibration': 'guest'},
        {'model': 'lattice', 'level': 'lattice_rss_proxy',
         'target': 'sampled_peak_rss_bytes',
         'measurement': 'ebpf_sampled_distinct_mm_rss_peak',
         'semantics': 'proxy', 'host_calibration': 'lattice_rss'},
        {'model': 'tool', 'level': 'tool_guest_extra',
         'target': 'memory_extra_peak_bytes',
         'measurement': 'guest_memtotal_minus_memavailable',
         'semantics': 'exact', 'host_calibration': 'guest'},
    ]
    calibrations = {
        key: {'target': 'host_cgroup_memory_current_increment',
              'sample_count': 3, 'scale': 1, 'offset_bytes': 0}
        for key in ('guest', 'lattice_rss')
    }
    selected = select_cold_start_p50({
        'lattice': rss_model('lattice', 5 * 2**20),
        'tool': model('runtime', 1 * 2**20),
    }, policy, calibrations)
    assert selected['fallback_level'] == 'lattice_rss_proxy'
    assert selected['prediction_semantics'] == 'proxy'
    assert selected['prediction_signal_target'] == 'sampled_peak_rss_bytes'
    assert selected['predicted_signal_memory_mib'] == 5


def test_toolkb_fallback_keeps_measurement_contract():
    tool = model('runtime', 2**21)
    assert select_p50({'tool': tool})['predicted_incremental_memory_mib'] == 2
    tool['memory_measurement'] = 'cgroup_v2_memory_current'
    with pytest.raises(PredictionUnavailable):
        select_p50({'tool': tool})


def test_tool_class_is_last_measured_fallback():
    coarse = model('runtime', 3 * 2**20)
    result = select_p50({'tool_class': coarse})
    assert result['predicted_incremental_memory_mib'] == 3
    assert result['fallback_level'] == 'tool_class'
    assert set(result['unavailable_models']) == {'lattice', 'exact_command', 'tool'}

    direct = model('runtime', 2 * 2**20)
    assert select_p50({'tool': direct, 'tool_class': coarse})['fallback_level'] == 'tool'


def test_exact_command_measurement_precedes_coarse_toolkb_fallback():
    exact = model('runtime', 7 * 2**20)
    coarse = model('runtime', 2 * 2**20)
    result = select_p50({'exact_command': exact, 'tool': coarse})
    assert result['predicted_incremental_memory_mib'] == 7
    assert result['fallback_level'] == 'exact_command'


def test_lightweight_requires_measured_short_duration_and_sampling_reason():
    good = {'count': 2, 'max_duration_ms': 9, 'reason': 'no_in_execution_memory_sample'}
    with pytest.raises(PredictionUnavailable):
        select_p50({}, short_call_evidence=good)
    for change in [{'count': 0}, {'max_duration_ms': 21}, {'reason': 'memory_sampling_gap'}, {'max_duration_ms': float('nan')}]:
        with pytest.raises(PredictionUnavailable):
            select_p50({}, short_call_evidence={**good, **change})


def test_frozen_provider_checks_repository_command_and_uses_training_prediction(tmp_path):
    path = tmp_path/'p50.json'
    path.write_text(json.dumps({'host_calibration': {'target': 'host_cgroup_memory_current_increment', 'sample_count': 3, 'scale': 1, 'offset_bytes': 0}, 'schema': 'clawbox_prediction_v2', 'repository': 'r', 'training_validated': True,
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
    identity = {'template_id': 'tpl-training', 'image_digest': 'sha256:training',
                'vcpu': 2, 'memory_mib': 4096,
                'architecture': 'arm64'}
    payload = {'host_calibration': {'target': 'host_cgroup_memory_current_increment', 'sample_count': 3, 'scale': 1, 'offset_bytes': 0}, 'schema': 'clawbox_prediction_v2', 'repository': 'r',
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


def test_prediction_applies_host_calibration_and_rejects_missing_mapping(tmp_path):
    path = tmp_path / 'calibrated.json'
    payload = {'schema': 'clawbox_prediction_v2', 'repository': 'r', 'training_validated': True,
        'host_calibration': {'target': 'host_cgroup_memory_current_increment',
                             'sample_count': 5, 'scale': 2, 'offset_bytes': 3 * 2**20},
        'commands': [{'command': 'pytest', 'models': {'tool': model('runtime', 7 * 2**20)}}]}
    path.write_text(json.dumps(payload))
    provider = P50PredictionProvider(path, repository='r')
    value = provider.resolve('pytest', next(iter(provider.manifest.values())))
    assert value['predicted_guest_extra_memory_mib'] == 7
    assert value['predicted_incremental_memory_mib'] == 17
    del payload['host_calibration']
    path.write_text(json.dumps(payload))
    with pytest.raises(PredictionUnavailable, match='host cgroup calibration'):
        P50PredictionProvider(path, repository='r')
