"""Public configuration must describe the settings the worker actually uses."""
from pathlib import Path

import pytest
import yaml

from experiment_fixtures import simulator_configuration
from clawbox.experiments.configuration import (
    BaselineConfig, WorkloadConfig, compile_configuration, dump_experiment,
    require_configured_node,
)
from clawbox.experiments.design import design_overview
from clawbox.experiments.qualification import qualification_spec
from clawbox.experiments.spec import InferenceSpec, expand_matrix, load_experiment, resolve_case_sandbox
from clawbox.experiments.worker import ExperimentWorker, session_worker_count


ROOT = Path(__file__).resolve().parents[1]


def documents():
    return [yaml.safe_load((ROOT / path).read_text(encoding='utf-8')) for path in (
        'examples/baselines/neutral.yaml', 'examples/workloads/smoke.yaml')]


def compile_pair(b, w):
    return compile_configuration(BaselineConfig.model_validate(b), WorkloadConfig.model_validate(w))


@pytest.mark.parametrize('count,grouped,window', [(4, False, 60.), (1, False, None), (4, True, None)])
def test_api_poisson_needs_no_duration_for_explicit_window_or_single_group(count, grouped, window):
    from clawbox.experiments.inputs import validate_launch_inputs
    from clawbox.experiments.worker import session_offer_offsets
    b, w = documents()
    b['inference'] = dict(backend='api', configuration=dict(model='test', base_url='https://example.invalid/v1'))
    w['tasks'].update(source='synthetic', input='inline-cases',
        session_assignment='grouped' if grouped else 'single_case',
        rollouts_per_case=count if grouped else 1,
        cases=[dict(case_id='live', source='synthetic', source_reference='opaque-live-task-id', prompt='Run a task.')])
    w['submission'].update(concurrency_levels=[count], arrival_schedule='poisson', poisson_arrival_window_seconds=window)
    spec = compile_pair(b, w)
    assert validate_launch_inputs(spec)['traces'] == []
    offsets = session_offer_offsets(expand_matrix(spec)[0])
    assert len(offsets) == count and offsets[0] == 0.
    if window is None:
        assert offsets == (0.,) * count
    else:
        assert all(a < b for a, b in zip(offsets, offsets[1:]))
        # Explicit windows retain the sequence used when durations existed,
        # and do not depend on replay scaling or automatic-window settings.
        w['tasks']['cases'][0]['metadata'] = {'recorded_duration_seconds': 123.}
        w['submission']['poisson_arrival_window_fraction'] = 99.
        b['inference']['configuration']['time_scale'] = 2.
        assert session_offer_offsets(expand_matrix(compile_pair(b, w))[0]) == offsets


@pytest.mark.parametrize('settings,remedy', [
    ({'misspelled_unknown_knob': 7}, 'extra_forbidden'),
    ({'qualification_max_parallel_sessions': 1}, 'max_active_sessions'),
    ({'command_timeout_seconds': 42}, 'execution.command_timeout_seconds'),
    ({'api_key': 'example-not-a-secret'}, 'api_key_env'),
    ({'clawtune_config': 'runner.yaml', 'model': 'override'}, 'remove duplicate'),
    ({'clawtune_root': '/src'}, 'requires clawtune_config'),
    ({'time_scale': True}, 'float_type'),
    ({'time_scale': float('nan')}, 'finite_number'),
    ({'qualification_case_limit': 0}, 'greater_than_equal'),
    ({'runtime_startup_timeout_seconds': 3601}, 'less_than_equal'),
    ({'runtime_completion_watch_seconds': 1.5}, 'int_type'),
    ({'openclaw_exec_yield_ms': 9}, 'greater_than_equal'),
    ({'serving_sim_diagnostic_detail': 'compact'}, 'require inference.backend'),
])
def test_invalid_inference_rejected_before_execution(settings, remedy):
    with pytest.raises(ValueError, match=remedy):
        InferenceSpec(configuration=settings)


def test_schema_exposes_inference_and_launch_without_injecting_defaults():
    schema = BaselineConfig.model_json_schema()
    config = schema['$defs']['InferenceConfiguration']
    launch = schema['$defs']['ServingSimLaunchConfiguration']
    assert config['additionalProperties'] is False
    assert launch['additionalProperties'] is False
    assert config['properties']['time_scale']['default'] == 1.0
    assert launch['properties']['forecast_interval_seconds']['default'] == .5
    assert 'cpu_affinity' in launch['required']
    assert schema['$defs']['InferenceSpec']['allOf'][0]['then']['properties']['configuration']['required'] == [
        'serving_sim_launch', 'base_url', 'model', 'serving_sim_catalog_manifest']
    assert InferenceSpec().configuration == {}
    config = simulator_configuration()
    assert InferenceSpec(backend='serving_sim', configuration=config).model_dump(mode='json')['configuration'] == config
    config['serving_sim_launch']['typo'] = 1
    with pytest.raises(ValueError, match='extra_forbidden'):
        InferenceSpec(backend='serving_sim', configuration=config)


def test_serving_sim_requires_managed_launch_and_valid_cross_field_contract():
    with pytest.raises(ValueError, match='requires.*serving_sim_launch'):
        InferenceSpec(backend='serving_sim')
    config = simulator_configuration()
    config['serving_sim_launch'].update(forecast_interval_seconds=3, forecast_validity_seconds=2)
    with pytest.raises(ValueError, match='interval must be below'):
        InferenceSpec(backend='serving_sim', configuration=config)


@pytest.mark.parametrize('key,value', [
    ('cores', True), ('cluster_count', 1.5), ('cpu_affinity', [True]),
    ('forecast_interval_seconds', True), ('startup_timeout_seconds', float('inf')),
])
def test_launch_does_not_coerce_invalid_numeric_settings(key, value):
    config = simulator_configuration()
    config['serving_sim_launch'][key] = value
    with pytest.raises(ValueError, match=key):
        InferenceSpec(backend='serving_sim', configuration=config)


def test_serving_sim_rejects_replay_time_scaling_before_input_checks():
    with pytest.raises(ValueError, match='cannot scale recorded delays'):
        InferenceSpec(backend='serving_sim', configuration=simulator_configuration(time_scale=2.))


@pytest.mark.parametrize('section,setting,remedy', [
    ('measurement', {'output': {'directory': '/ignored'}}, '--output-root'),
    ('evidence', {'idle_reclaim_costs': {}}, 'unused by context-first'),
])
def test_unused_public_fields_have_actionable_errors(section, setting, remedy):
    b, _ = documents()
    b.setdefault(section, {}).update(setting)
    with pytest.raises(ValueError, match=remedy):
        BaselineConfig.model_validate(b)


def test_removed_pressure_cost_is_not_a_policy_option():
    from clawbox.experiments.spec import PressureControlSpec
    with pytest.raises(ValueError, match='amortization_factor'):
        PressureControlSpec(amortization_factor=2)


@pytest.mark.parametrize('access', [False, True])
def test_case_template_has_no_network_override_and_freezes_with_baseline_policy(tmp_path, access):
    b, w = documents()
    b['environment']['sandbox']['allow_internet_access'] = access
    w['tasks']['cases'][0]['sandbox'] = dict(template_id='case-template', memory_mib=4096)
    spec = compile_pair(b, w)
    path = tmp_path / 'frozen.yaml'
    path.write_text(dump_experiment(spec), encoding='utf-8')
    loaded = load_experiment(path)
    template = loaded.workload.cases[0]
    resolved = resolve_case_sandbox(template, loaded.sandbox)
    assert (resolved.template, resolved.memory_mib, resolved.allow_internet_access) == ('case-template', 4096, access)
    assert 'allow_internet_access' not in WorkloadConfig.model_json_schema()['$defs']['TemplateSpec']['properties']
    w['tasks']['cases'][0]['sandbox']['allow_internet_access'] = access
    with pytest.raises(ValueError, match='environment.sandbox.allow_internet_access'):
        WorkloadConfig.model_validate(w)


def test_qualification_active_cap_roundtrips_without_mutating_formal_workload(tmp_path):
    b, w = documents()
    w['submission'].update(concurrency_levels=[4], max_active_sessions=3)
    w['tasks']['session_assignment'] = 'single_case'
    spec = compile_pair(b, w)
    qualified = qualification_spec(spec, concurrency=2)
    assert qualified.execution.max_active_sessions == 2
    assert 'qualification_max_parallel_sessions' not in qualified.inference.configuration
    path = tmp_path / 'qualification.yaml'
    path.write_text(dump_experiment(qualified), encoding='utf-8')
    assert session_worker_count(expand_matrix(load_experiment(path))[0]) == 2
    assert session_worker_count(expand_matrix(spec)[0]) == 3
    assert qualification_spec(spec, concurrency=4).execution.max_active_sessions == 3


def test_conflicting_worker_node_fails_before_client_or_output_creation(tmp_path, monkeypatch):
    b, w = documents()
    spec = compile_pair(b, w)
    monkeypatch.setenv('CLAWBOX_WORKER_NODE', 'different-node')
    output = tmp_path / 'unused'
    with pytest.raises(ValueError, match='unset CLAWBOX_WORKER_NODE'):
        ExperimentWorker(spec, run_id='check', attempt_id='one', task_uid='one', output_root=output)
    assert not output.exists()
    monkeypatch.setenv('CLAWBOX_WORKER_NODE', spec.resources.target_node)
    assert require_configured_node(spec.resources.target_node) == spec.resources.target_node
    monkeypatch.delenv('CLAWBOX_WORKER_NODE')
    assert require_configured_node(spec.resources.target_node) == spec.resources.target_node


def test_describe_reports_actual_cluster_cpu_layout_and_ownership():
    b, w = documents()
    config = simulator_configuration()
    config['serving_sim_launch'].update(cpu_affinity=[8, 9], cores=2, cluster_count=3)
    b['inference'] = dict(backend='serving_sim', configuration=config)
    report = design_overview(compile_pair(b, w))
    assert [c['cpu_affinity'] for c in report['simulator']['clusters']] == [[8], [9], [8]]
    assert [c['cores'] for c in report['simulator']['clusters']] == [1, 1, 1]
    assert report['simulator']['active_cpu_ids'] == [8, 9]
    assert report['simulator']['shared_cpus'] is True
    assert report['configuration_ownership']['output_root'] == 'CLI --output-root > CLAWBOX_OUTPUT_ROOT > results'


def test_host_readiness_rejects_mismatched_sharing_evidence_before_probes(monkeypatch):
    from clawbox.experiments import qualification
    b, w = documents()
    b['mechanisms']['placement']['template_affinity'] = True
    b['mechanisms']['placement']['policy'] = 'load_aware'
    b['environment']['resources'].update(local_memory_cgroup='/sys/fs/cgroup/clawbox', pool_memory_budget_mib=12288,
        compute_nodes=[dict(node_id='node0', numa_node=0, cpus='0-3',
                           memory_capacity_mib=16384, low_watermark_mib=8192,
                           high_watermark_mib=12288)])
    b['evidence']['template_base_sharing_artifact'] = '/wrong-cohort-proof.json'
    spec = compile_pair(b, w)
    def mismatch(effective, path):
        assert effective.resources.template_base_sharing == 'required'
        assert path == Path('/wrong-cohort-proof.json')
        raise ValueError('template-sharing artifact does not match the exact Tool templates')
    monkeypatch.setattr(qualification, 'validate_template_base_sharing_artifact', mismatch)
    monkeypatch.setattr('clawbox.experiments.base_cache.check_experiment',
                        lambda _: pytest.fail('must reject bad evidence before further host operations'))
    with pytest.raises(ValueError, match='prove-template-sharing.*--resume-result'):
        qualification.require_host_ready(spec, launch_checks=False)
