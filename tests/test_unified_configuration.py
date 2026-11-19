from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from clawbox import cli
from clawbox.experiments.configuration import (
    BaselineConfig, WorkloadConfig, compile_configuration, dump_experiment, load_configuration,
)
from clawbox.experiments.spec import expand_matrix, load_experiment, spec_digest
from clawbox.experiments.prediction import resolve_command_prediction, PredictionUnavailable
from clawbox.experiments.priority import tool_duration_prediction
from clawbox.experiments.policy import PolicyCoordinator


ROOT = Path(__file__).resolve().parents[1]


def documents():
    baseline = yaml.safe_load((ROOT / 'examples/baselines/neutral.yaml').read_text())
    workload = yaml.safe_load((ROOT / 'examples/workloads/smoke.yaml').read_text())
    resources = baseline['environment']['resources']
    resources.update(local_memory_capacity_mib=16384, local_memory_low_watermark_mib=8192,
                     local_memory_high_watermark_mib=12288, pool_memory_budget_mib=12288,
                     local_numa_node=0, warm_numa_node=1, warm_snapshot_root='/mnt/warm',
                     warm_memory_capacity_mib=8192, shared_memory_borrow_limit_mib=4096)
    return baseline, workload


def compile_pair(baseline, workload):
    return compile_configuration(BaselineConfig.model_validate(baseline),
                                 WorkloadConfig.model_validate(workload))


def test_two_files_are_cwd_independent_and_freeze_roundtrip(tmp_path, monkeypatch):
    spec = load_configuration(ROOT / 'examples/baselines/neutral.yaml', ROOT / 'examples/workloads/smoke.yaml')
    monkeypatch.chdir(tmp_path)
    same = load_configuration(ROOT / 'examples/baselines/neutral.yaml', ROOT / 'examples/workloads/smoke.yaml')
    assert spec == same
    assert Path(spec.workload.cases[0].replay_trace_reference).is_file()
    frozen = tmp_path / 'experiment.yaml'
    frozen.write_text(dump_experiment(spec), encoding='utf-8')
    loaded = load_experiment(frozen)
    assert spec_digest(spec) == spec_digest(loaded)
    assert expand_matrix(spec) == expand_matrix(loaded)


@pytest.mark.parametrize('version', [3, 4])
def test_retired_public_inputs_are_rejected(tmp_path, version):
    path = tmp_path / 'old.yaml'
    path.write_text(yaml.safe_dump({'schema_version': version}))
    with pytest.raises(ValueError, match='--baseline'):
        load_experiment(path)


def test_branching_source_paths_survive_cwd_change_and_freeze(tmp_path, monkeypatch):
    from clawbox.experiments.configuration import SUBMISSION_FIELDS
    from clawbox.experiments.inputs import validate_inputs

    raw = yaml.safe_load((ROOT / 'tests/fixtures/experiments/'
                         'snapshot-scheduling-correctness-kunpeng.yaml').read_text())
    baseline = {key: deepcopy(value) for key, value in raw.items()
                if key not in {'schema_version', 'experiment_id', 'workload', 'comparison'}}
    baseline.update(kind='clawbox-baseline', name='branching')
    execution = baseline['execution']
    submission = {key: execution.pop(key) for key in list(execution)
                  if key in SUBMISSION_FIELDS}
    workload = dict(kind='clawbox-workload', name='branching', tasks=raw['workload'],
                    submission=submission, validation=baseline['measurement'].pop('validation'))
    configs = tmp_path / 'configs'
    configs.mkdir()
    scenario = configs / 'scenario.json'
    scenario.write_bytes((ROOT / 'examples/workloads/snapshot-branching.json').read_bytes())
    workload['tasks']['input'] = 'scenario.json'
    workload['tasks']['cases'][0]['source_reference'] = 'scenario.json'
    baseline_path, workload_path = configs / 'baseline.yaml', configs / 'workload.yaml'
    baseline_path.write_text(yaml.safe_dump(baseline))
    workload_path.write_text(yaml.safe_dump(workload))

    monkeypatch.chdir(tmp_path)
    spec = load_configuration(baseline_path, workload_path)
    assert spec.workload.cases[0].source_reference == str(scenario)
    report = validate_inputs(spec)
    assert report['traces'][0]['path'] == str(scenario)
    frozen = tmp_path / 'resolved.yaml'
    frozen.write_text(dump_experiment(spec), encoding='utf-8')
    assert validate_inputs(load_experiment(frozen)) == report

    pair = ['--baseline', str(baseline_path), '--workload', str(workload_path)]
    for _ in range(2):
        assert cli.main(['experiment', 'validate', *pair, '--inputs']) == 0
    scenario.unlink()
    assert cli.main(['experiment', 'validate', *pair, '--inputs']) == 1


def test_files_have_disjoint_ownership_and_no_overrides():
    b, w = documents()
    for patch in ({'comparison': {'arms': []}}, {'workload': {}}, {'execution': {'concurrency_levels': [3]}}):
        with pytest.raises(ValueError):
            BaselineConfig.model_validate(b | patch)
    with pytest.raises(ValueError):
        WorkloadConfig.model_validate(w | {'mechanisms': {}})


def test_non_file_source_reference_is_preserved(tmp_path):
    baseline, workload = documents()
    workload['tasks']['source'] = 'synthetic'
    case = workload['tasks']['cases'][0]
    case.update(source='synthetic', source_reference='generator:marker-v1')
    baseline_path, workload_path = tmp_path / 'baseline.yaml', tmp_path / 'workload.yaml'
    baseline_path.write_text(yaml.safe_dump(baseline))
    workload_path.write_text(yaml.safe_dump(workload))
    spec = load_configuration(baseline_path, workload_path)
    assert spec.workload.cases[0].source_reference == 'generator:marker-v1'


def test_saved_install_limit_is_not_reset():
    b, w = documents()
    b['execution']['dependency_install_concurrency'] = 3
    assert compile_pair(b, w).execution.dependency_install_concurrency == 3


def test_cache_with_resident_vms_preserves_mapping_mechanism():
    b, w = documents()
    b['environment']['resources']['base_template_cache'] = 'required'
    spec = compile_pair(b, w)
    assert spec.resources.snapshot_mechanism == 'incremental-cow'
    assert spec.policies[0].reclamation.value == 'resident'


@pytest.mark.parametrize('placement', ['affinity', 'snapshot_aware'])
def test_specialized_placement_is_rejected_in_ordinary_worker(placement):
    b, w = documents()
    b['mechanisms']['placement']['policy'] = placement
    with pytest.raises(ValueError, match='snapshot_branching'):
        compile_pair(b, w)


@pytest.mark.parametrize('admission', ['off', 'fixed', 'predicted'])
def test_b_uses_available_duration_independently_of_memory_admission(admission):
    from clawbox.experiments.spec_types import AdmissionPolicy
    arm = SimpleNamespace(policy=SimpleNamespace(admission=AdmissionPolicy(admission)),
                          execution=SimpleNamespace(tool_call_cpu_policy='b'),
                          resources=SimpleNamespace(prediction_mode='host-live'))
    prediction = {'prediction_source': 'host_live_clawtune', 'models': {'tool': {
        'targets': {'duration_ms': {'status': 'available', 'p50': 5000, 'unit': 'ms',
                                  'metric_definition': 'retained_workload_elapsed'}}}}}
    resolved = resolve_command_prediction(arm, {'prediction': prediction}, object())
    assert tool_duration_prediction(resolved)['predicted_duration_seconds'] == 5


def test_b_missing_provider_is_an_error_not_silent_disable():
    from clawbox.experiments.spec_types import AdmissionPolicy
    arm = SimpleNamespace(policy=SimpleNamespace(admission=AdmissionPolicy.OFF),
                          execution=SimpleNamespace(tool_call_cpu_policy='b'))
    with pytest.raises(PredictionUnavailable, match='prediction provider'):
        resolve_command_prediction(arm, {}, None)


@pytest.mark.parametrize('cpu_policy', ['c2', 'b'])
def test_measurement_frequency_does_not_set_cpu_control_frequency(cpu_policy):
    from clawbox.experiments.spec import ExecutionSpec
    execution = ExecutionSpec(tool_call_cpu_policy=cpu_policy,
                              memory_sample_interval_seconds=2,
                              tool_call_cpu_poll_interval_seconds=.5)
    assert execution.llc_poll_interval_seconds == .5


@pytest.mark.parametrize('reclaim', ['wait', 'gpu'])
@pytest.mark.parametrize('scope', ['tool', 'agent_and_tool'])
def test_gpu_signal_consumers_are_independent_of_reclaim_choice(reclaim, scope):
    b, w = documents()
    b['inference']['backend'] = 'serving_sim'
    b['mechanisms']['idle_reclaim'].update(policy=reclaim, scope=scope)
    b['mechanisms']['checkpoint'].update(storage='tiered', residency='next-use')
    b['mechanisms']['restore']['prewarm_events'] = ['think_end']
    spec = compile_pair(b, w)
    assert spec.policies[0].snapshot_next_use_residency
    assert spec.policies[0].consumes_gpu_signals
    assert spec.policies[0].runtime_snapshot == (scope == 'agent_and_tool')


@pytest.mark.parametrize('events,expected', [([], False), (['think_end'], True), (['first_token'], False)])
def test_prewarm_can_be_independently_disabled_with_wait_reclaim(events, expected):
    from test_gpu_policy import Lifecycle
    b, w = documents()
    b['inference']['backend'] = 'serving_sim'
    b['mechanisms']['idle_reclaim'].update(policy='wait', scope='tool')
    b['mechanisms']['restore']['prewarm_events'] = events
    policy = compile_pair(b, w).policies[0]
    c = PolicyCoordinator(policy, budget_mib=32, emergency_free_mib=0, operation_headroom_mib=0)
    c.register('s', Lifecycle())
    c.begin_model_wait('s', 'r')
    result = c.observe_gpu_signal('s', 'r', dict(event='think_end', role='A', source='trace'))
    assert result['should_prewarm'] is expected


@pytest.mark.parametrize('compression', ['none', 'zstd-page'])
def test_page_preparation_allows_uncompressed_wait_tool_snapshots(compression):
    b, w = documents()
    b['inference']['backend'] = 'serving_sim'
    b['mechanisms']['idle_reclaim'].update(policy='wait', scope='tool')
    b['environment']['resources']['snapshot_compression'] = compression
    b['execution']['snapshot_page_management'] = {'prefetch_budget_mib': 128}
    spec = compile_pair(b, w)
    assert spec.policies[0].consumes_gpu_signals


def test_borrow_only_does_not_require_unused_cold_costs():
    b, w = documents()
    b['mechanisms']['shared_memory']['borrow'] = 'pressure'
    b['mechanisms']['coordination']['action_selection'] = 'measured_cost'
    b['evidence']['memory_action_costs'] = dict(provenance='synthetic-unit-test',
        live_borrow_service_seconds_per_gib=1, live_borrow_runtime_penalty_seconds_per_gib=0,
        warm_checkpoint_seconds_per_gib=1, warm_restore_seconds_per_gib=1,
        expected_restore_probability=.5)
    spec = compile_pair(b, w)
    assert spec.resources.memory_action_costs.cold_restore_seconds_per_gib is None
    b['mechanisms']['idle_reclaim']['policy'] = 'wait'
    with pytest.raises(ValueError, match='COLD'):
        compile_pair(b, w)


def test_public_cli_pair_resolve_validate_repeat_and_missing_input(tmp_path, capsys):
    pair = ['--baseline', str(ROOT / 'examples/baselines/neutral.yaml'),
            '--workload', str(ROOT / 'examples/workloads/smoke.yaml')]
    for _ in range(2):
        assert cli.main(['experiment', 'validate', *pair, '--inputs']) == 0
    output = tmp_path / 'resolved.yaml'
    assert cli.main(['experiment', 'resolve', *pair, '--output', str(output)]) == 0
    before = output.read_bytes()
    assert cli.main(['experiment', 'resolve', *pair, '--output', str(output)]) == 1
    assert before == output.read_bytes()
    assert cli.main(['experiment', 'validate', str(output), '--inputs']) == 0
    assert cli.main(['experiment', 'validate', '--baseline', pair[1]]) == 1
    assert 'both --baseline and --workload' in capsys.readouterr().err


@pytest.mark.parametrize('reclaim', ['wait_aware_pressure', 'gpu_aware_pressure'])
@pytest.mark.parametrize('pair_scope', [False, True])
@pytest.mark.parametrize('enabled', [False, True])
def test_actual_worker_prewarm_callbacks_obey_independent_choices(reclaim, pair_scope, enabled):
    from test_serving_sim_gateway import worker_prewarm_environment
    env, restored, events, executor = worker_prewarm_environment(
        reclaim=reclaim, runtime_snapshot=pair_scope,
        prewarm_events=('think_end',) if enabled else ())
    env['runtime_lifecycle'].resident = not pair_scope
    try:
        env['on_model_signal']({'session_id': 'session', 'request_id': 'request',
            'event': 'think_end', 'role': 'A', 'source': 'trace', 't': 2.})
    finally:
        executor.close()
    executor.check()
    assert restored == ((['runtime', 'tool'] if pair_scope else ['tool']) if enabled else [])


@pytest.mark.parametrize('patch', [{'evidence': []}, {'execution': None}, {'inference': []}])
def test_malformed_sections_raise_validation_errors_not_attribute_errors(tmp_path, patch):
    b, w = documents()
    baseline, workload = tmp_path / 'b.yaml', tmp_path / 'w.yaml'
    baseline.write_text(yaml.safe_dump(b | patch), encoding='utf-8')
    workload.write_text(yaml.safe_dump(w), encoding='utf-8')
    with pytest.raises(ValueError):
        load_configuration(baseline, workload)


def test_absolute_target_host_paths_are_preserved():
    from clawbox.experiments.configuration import _file
    assert _file('/data/seed.json', ROOT) == '/data/seed.json'
