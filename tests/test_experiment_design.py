from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from clawbox.experiments.configure import configure_experiment, dump_experiment
from clawbox.experiments.design import (DIMENSIONS, GROUPS, Design, Mechanisms, compile_design,
                                       export_design, recipe_arms, design_overview)
from clawbox.experiments.spec import ExperimentSpec, effective_spec, expand_matrix, spec_digest


def base():
    raw = yaml.safe_load(Path('examples/experiments/agent-rl-heterogeneous-ablation-kunpeng.yaml').read_text(encoding='utf-8'))
    # Host-specific fixture supplies a topology, not empirical assertions for
    # these unit tests. None of these synthetic costs are used for live runs.
    from clawbox.experiments.design import EnvironmentResources
    r = raw['resources']
    return dict(schema_version=4, experiment_id='design-test',
                environment=dict(runtime=raw['runtime'], sandbox=raw['sandbox'],
                                 resources={k: v for k, v in r.items() if k in EnvironmentResources.model_fields}),
                workload=raw['workload'], inference=dict(backend='replay', configuration={'model': 'test'}),
                mechanisms=Mechanisms().model_dump(mode='json'),
                evidence=dict(prediction_artifact='/separate-training/prediction.json',
                    snapshot_residency_costs=dict(provenance='unit-test', warm_restore_seconds_per_gib=1, cold_restore_seconds_per_gib=2),
                    idle_reclaim_costs=dict(provenance='unit-test', warm_checkpoint_seconds_per_gib=1,
                        warm_restore_seconds_per_gib=1, cold_checkpoint_seconds_per_gib=2,
                        cold_restore_seconds_per_gib=2),
                    memory_action_costs=dict(provenance='unit-test', live_borrow_service_seconds_per_gib=1,
                        live_borrow_runtime_penalty_seconds_per_gib=0, warm_checkpoint_seconds_per_gib=1,
                        warm_restore_seconds_per_gib=1, cold_checkpoint_seconds_per_gib=2,
                        cold_restore_seconds_per_gib=2, expected_restore_probability=.5)),
                execution=dict(concurrency_levels=[2], randomized_order=False),
                comparison=dict(recipe='custom', arms=[dict(name='custom')]))


def design(recipe='custom', axis=None):
    raw = base()
    m = raw['mechanisms']
    m['admission']['static_tool_memory_mib'] = 128
    if recipe in {'shared-memory', 'coordination', 'checkpoint', 'integrated'}:
        m['idle_reclaim']['policy'] = 'wait'
        m['checkpoint']['storage'] = 'tiered'
    if recipe == 'coordination':
        m['shared_memory']['borrow'] = 'pressure'
    if recipe == 'integrated':
        m['placement'].update(policy='load_aware', template_affinity=True)
        m['runtime'].update(memory_protection=True, cpu_release_boost=True)
        m['shared_memory']['borrow'] = 'pressure'
        m['checkpoint']['residency'] = 'value'
    raw['comparison'] = dict(recipe=recipe, arms=recipe_arms(recipe, checkpoint_axis=axis))
    return ExperimentSpec.model_validate(raw)


@pytest.mark.parametrize('recipe,axis,count', [
    ('admission', None, 3), ('runtime', None, 4), ('placement', None, 3),
    ('idle-reclaim', None, 3), ('shared-memory', None, 3), ('coordination', None, 2), ('memory-reclaim', None, 2),
    ('checkpoint', 'encoding', 2), ('checkpoint', 'storage', 3),
    ('checkpoint', 'residency', 2), ('integrated', None, 3), ('custom', None, 1),
])
def test_every_recipe_roundtrips_and_expands(recipe, axis, count):
    spec = design(recipe, axis)
    assert len(spec.policies) == count
    loaded = ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(spec)))
    assert loaded == spec
    assert spec_digest(loaded) == spec_digest(spec)
    arms = expand_matrix(spec)
    assert len(arms) == count * spec.workload.repetitions
    assert {a.arm_id for a in arms} == {a.arm_id for a in expand_matrix(loaded)}
    assert all(set(p.mechanisms) == set(GROUPS) for p in spec.policies)


def test_c2_tool_call_cpu_policy_roundtrips_and_rejects_os_placement():
    raw = base()
    raw['mechanisms']['placement'].update(
        tool_call_cpu_policy='c2', tool_call_cpu_pressure_ratio=.2,
        tool_call_cpu_pressure_samples=3,
    )
    spec = ExperimentSpec.model_validate(raw)
    arm = expand_matrix(spec)[0]
    assert arm.execution.tool_call_cpu_policy == 'c2'
    assert arm.execution.tool_call_cpu_pressure_ratio == .2
    assert ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(spec))) == spec

    raw['mechanisms']['placement']['policy'] = 'os_default'
    with pytest.raises(ValueError, match='C2 Tool-call CPU placement'):
        ExperimentSpec.model_validate(raw)


def test_removed_c2_cooldown_field_is_rejected():
    raw = base()
    raw['mechanisms']['placement']['tool_call_cpu_cooldown_seconds'] = 3
    with pytest.raises(ValueError, match='Extra inputs are not permitted'):
        ExperimentSpec.model_validate(raw)


def test_c0_and_c2_effective_arms_differ_only_in_cpu_policy():
    raw = base()
    raw['comparison']['arms'] = [
        {'name': 'c0'},
        {'name': 'c2', 'override': {'placement.tool_call_cpu_policy': 'c2'}},
    ]
    c0, c2 = ExperimentSpec.model_validate(raw).policies
    c0_execution = dict(c0.execution_overrides)
    c2_execution = dict(c2.execution_overrides)
    assert c0_execution.pop('tool_call_cpu_policy') == 'c0'
    assert c2_execution.pop('tool_call_cpu_policy') == 'c2'
    assert c0_execution == c2_execution
    assert c0.resource_overrides == c2.resource_overrides


def test_recipe_and_arm_names_do_not_change_execution():
    raw = export_design(design('runtime'))
    before = expand_matrix(ExperimentSpec.model_validate(raw))
    raw['comparison']['recipe'] = 'admission'
    for i, p in enumerate(raw['comparison']['arms']):
        p['name'] = 'renamed-'+str(i)
    after = expand_matrix(ExperimentSpec.model_validate(raw))
    assert [(a.resources, a.execution, a.policy.mechanisms) for a in before] == [
        (a.resources, a.execution, a.policy.mechanisms) for a in after]


@pytest.mark.parametrize('switch,value', [('idle_reclaim.policy', 'off'), ('shared_memory.borrow', 'off')])
def test_full_single_capability_ablation_retains_cost_coordination(switch, value):
    raw = base()
    raw['mechanisms']['placement']['policy'] = 'load_aware'
    raw['mechanisms']['runtime'].update(memory_protection=True, cpu_release_boost=True)
    raw['mechanisms']['admission']['tool'] = 'predicted'
    raw['mechanisms']['shared_memory']['borrow'] = 'pressure'
    raw['mechanisms']['idle_reclaim']['policy'] = 'wait'
    raw['mechanisms']['checkpoint']['storage'] = 'tiered'
    raw['mechanisms']['coordination']['action_selection'] = 'measured_cost'
    raw['comparison']['arms'] = [{'name': 'full'}, {'name': 'ablation', 'override': {switch: value}}]
    spec = ExperimentSpec.model_validate(raw)
    full, ablation = spec.policies
    differences = {f'{group}.{key}' for group in GROUPS for key in full.mechanisms[group]
                   if full.mechanisms[group][key] != ablation.mechanisms[group][key]}
    assert differences == {switch}
    assert ablation.mechanisms['coordination']['action_selection'] == 'measured_cost'
    assert effective_spec(spec, full).resources.physical_live_limit_mib == effective_spec(spec, ablation).resources.physical_live_limit_mib
    assert effective_spec(spec, ablation).resources.memory_action_costs is not None


def test_runtime_controls_do_not_enable_reclaim():
    spec = design('runtime')
    p = spec.policies[-1]
    assert p.memory_protection and p.cpu_release_boost
    assert not p.coordinates_release
    assert p.reclamation.value == 'resident'
    assert p.runtime_control.value == 'explicit'


def test_wait_checkpoint_does_not_require_llm_duration_evidence():
    raw = base()
    raw['mechanisms']['idle_reclaim'].update(policy='wait', scope='agent_and_tool')
    spec = ExperimentSpec.model_validate(raw)
    assert spec.policies[0].runtime_snapshot
    assert spec.inference.configuration.get('model_wait_prediction_seconds') is None

    raw['mechanisms']['idle_reclaim'].update(policy='release_cost', scope='tool')
    with pytest.raises(ValueError, match="Input should be 'off' or 'wait'"):
        ExperimentSpec.model_validate(raw)


def test_resident_arm_keeps_common_physical_shared_pool_capacity():
    raw = base()
    raw['mechanisms']['runtime'].update(memory_protection=True, cpu_release_boost=True)
    spec = ExperimentSpec.model_validate(raw)
    arm = expand_matrix(spec)[0]
    assert arm.resources.warm_memory_capacity_mib == raw['environment']['resources']['warm_memory_capacity_mib']
    assert not arm.resources.shared_memory_borrow_limit_mib
    assert arm.policy.reclamation.value == 'resident'


def test_runtime_forecast_ttl_roundtrips_to_every_arm():
    raw = export_design(design('runtime'))
    raw['mechanisms']['runtime']['pressure_control']['release_forecast_ttl_factor'] = 4.
    spec = ExperimentSpec.model_validate(raw)
    loaded = ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(spec)))
    assert all(a.resources.pressure_control.release_forecast_ttl_factor == 4.
               for a in expand_matrix(loaded))


def test_runtime_missing_psi_fails_before_host_or_vm_setup(tmp_path, monkeypatch):
    from clawbox.experiments import qualification
    raw = export_design(design('runtime'))
    raw['environment']['resources']['local_memory_cgroup'] = str(tmp_path)
    spec = ExperimentSpec.model_validate(raw)
    monkeypatch.setattr(qualification, 'host_profile_for',
                        lambda *_: pytest.fail('host setup before PSI validation'))
    with pytest.raises(RuntimeError, match='PSI.*psi=1'):
        qualification.require_host_ready(spec)


def test_os_only_design_checks_common_physical_shared_pool(monkeypatch):
    from clawbox.experiments import qualification
    raw = base()
    raw['mechanisms'].update(
        deployment={'mode': 'os-default'},
        coordination={'action_selection': 'reclaim_first'},
        admission={**raw['mechanisms']['admission'], 'tool': 'off'},
        runtime={**raw['mechanisms']['runtime'], 'memory_protection': False,
                 'cpu_release_boost': False, 'proactive_memory_reclaim': False},
        placement={**raw['mechanisms']['placement'], 'policy': 'os_default',
                   'template_affinity': False},
        idle_reclaim={**raw['mechanisms']['idle_reclaim'], 'policy': 'off'},
        shared_memory={**raw['mechanisms']['shared_memory'], 'borrow': 'off'},
    )
    raw['comparison']['arms'] = [{'name': 'os-default-no-admission'}]
    spec = ExperimentSpec.model_validate(raw)
    profiles = []
    monkeypatch.setattr(qualification, 'apply_environment', profiles.append)
    monkeypatch.setattr(qualification, 'doctor', lambda *_args, **_kwargs: [])

    qualification.require_host_ready(spec)

    assert profiles[0]['shared_memory_borrow_limit_mib'] == \
        raw['environment']['resources']['shared_memory_borrow_limit_mib']


def test_runtime_recipe_pins_proactive_reclaim_off():
    raw = export_design(design('runtime'))
    raw['mechanisms']['runtime']['proactive_memory_reclaim'] = True
    raw['comparison']['arms'] = recipe_arms('runtime')
    spec = ExperimentSpec.model_validate(raw)
    assert spec.policies[0].name == 'memory-off-cpu-off'
    assert all(not effective_spec(spec, p).resources.proactive_cache_reclaim for p in spec.policies)
    assert {(p.memory_protection, p.cpu_release_boost) for p in spec.policies} == {
        (False, False), (True, False), (False, True), (True, True)}


def test_memory_reclaim_recipe_changes_only_reclaim():
    spec = design('memory-reclaim')
    before, after = [effective_spec(spec, p).resources.model_dump() for p in spec.policies]
    assert {key for key in before if before[key] != after[key]} == {'proactive_cache_reclaim'}
    assert not before['proactive_cache_reclaim'] and after['proactive_cache_reclaim']


def test_affinity_automatically_requires_sharing_qualification():
    spec = design('placement')
    assert [effective_spec(spec, p).resources.template_base_sharing for p in spec.policies] == [
        'disabled', 'disabled', 'required']
    assert spec.policies[-1].template_aware_placement
    assert 'template_base_sharing' not in spec.policies[-1].mechanisms['placement']


def test_coordination_comparison_fixes_borrowing_and_reclamation():
    spec = design('coordination')
    a, b = spec.policies
    assert a.mechanisms['shared_memory'] == b.mechanisms['shared_memory']
    assert a.mechanisms['idle_reclaim'] == b.mechanisms['idle_reclaim']
    assert effective_spec(spec, a).resources.memory_action_costs is None
    assert effective_spec(spec, b).resources.memory_action_costs is not None


def test_prediction_source_is_a_shared_condition(tmp_path):
    raw = export_design(design('admission'))
    raw['conditions']['prediction_source'] = 'command-table'
    spec = ExperimentSpec.model_validate(raw)
    assert all(effective_spec(spec, p).resources.prediction_mode == 'frozen' for p in spec.policies)
    assert 'Diagnostic' in design_overview(spec)['arms'][0]['warnings'][0]
    assert ExperimentSpec.model_validate(export_design(spec)) == spec
    path = tmp_path/'base.yaml'
    path.write_text(dump_experiment(spec), encoding='utf-8')
    configured = configure_experiment(path, mechanism_settings=['conditions.prediction_source=kb'])
    assert all(effective_spec(configured, p).resources.prediction_mode == 'runtime' for p in configured.policies)
    raw['comparison']['arms'][0]['override']['conditions.prediction_source'] = 'kb'
    with pytest.raises(ValueError, match='mechanism field'):
        ExperimentSpec.model_validate(raw)


@pytest.mark.parametrize('field,value', [
    ('idle_reclaim.restore', 'reactive'), ('placement.template_base_sharing', 'required'),
    ('checkpoint.lazy_restore', False), ('admission.prediction_mode', 'frozen'),
    ('admission.lifecycle_admission', 'os-managed'), ('shared_memory.action_selection', 'reclaim_first'),
    ('runtime.proactive_cache_reclaim', True),
])
def test_removed_or_reclassified_fields_are_not_silently_accepted(field, value):
    raw = base()
    raw['comparison']['arms'][0].setdefault('override', {})[field] = value
    with pytest.raises(ValueError, match='unknown mechanism field'):
        ExperimentSpec.model_validate(raw)


def test_os_deployment_cannot_be_mixed_with_managed_optimization():
    raw = export_design(design('integrated'))
    raw['comparison']['arms'][0]['override']['runtime.proactive_memory_reclaim'] = True
    with pytest.raises(ValueError, match='os-default deployment requires'):
        ExperimentSpec.model_validate(raw)


def test_integrated_baselines_do_not_inherit_load_aware_placement():
    spec = design('integrated')
    os_arm, managed, configured = [effective_spec(spec, p) for p in spec.policies]
    assert os_arm.resources.lifecycle_admission == 'os-managed'
    assert os_arm.policies[0].deployment == 'os-default'
    assert managed.resources.lifecycle_admission == 'configured-capacity'
    assert managed.execution.placement_policy == 'round_robin'
    assert configured.execution.placement_policy == 'load_aware'


def test_restore_accounting_is_fixed_not_a_loading_switch():
    spec = design('checkpoint', 'encoding')
    for p in spec.policies:
        assert effective_spec(spec, p).resources.lazy_restore is True
        assert p.restore.value == 'reactive'
        assert 'lazy_restore' not in p.mechanisms['checkpoint']
        assert 'restore' not in p.mechanisms['idle_reclaim']
    raw = export_design(spec)
    raw['conditions']['restore_loading'] = 'eager'
    with pytest.raises(ValueError, match='Extra inputs'):
        ExperimentSpec.model_validate(raw)


def test_report_exposes_deployment_coordination_and_common_conditions(tmp_path):
    import json
    from clawbox.experiments.reporting import render_run_report
    spec = design('integrated')
    (tmp_path/'experiment.yaml').write_text(dump_experiment(spec), encoding='utf-8')
    (tmp_path/'arms').mkdir()
    (tmp_path/'arms'/'arm.json').write_text(json.dumps({
        'experiment_id': spec.experiment_id, 'status': 'succeeded',
        'arm': expand_matrix(spec)[0].model_dump(mode='json'),
        'correctness': {}, 'performance': {}, 'memory': {}, 'artifacts': {},
    }), encoding='utf-8')
    report = render_run_report(tmp_path)
    assert 'Deployment | Coordination' in report
    assert '"prediction_source": "kb"' in report
    assert 'configured-design' in report


def test_removed_controller_is_not_public_or_accepted(capsys):
    from clawbox import cli
    from clawbox.experiments.spec_types import RuntimeControl
    from clawbox.experiments.baselines import BASELINES
    import json
    assert 'agentcgroup' not in json.dumps(Design.model_json_schema()).lower()
    assert all('agentcgroup' not in name for name in BASELINES)
    for name in ('agentcgroup_cgroup', 'agentcgroup_legacy'):
        with pytest.raises(ValueError):
            RuntimeControl(name)
    for command in ('recipes', 'baselines', 'schema'):
        assert cli.main(['experiment', command]) == 0
        assert 'agentcgroup' not in capsys.readouterr().out.lower()
    with pytest.raises(SystemExit) as exc:
        cli.main(['experiment', 'configure', '--help'])
    assert exc.value.code == 0
    assert 'agentcgroup' not in capsys.readouterr().out.lower()


def test_pair_reclaim_does_not_enable_cpu_or_memory_controls():
    p = design('idle-reclaim').policies[-1]
    assert not p.coordinates_release
    assert not p.memory_protection and not p.cpu_release_boost
    assert p.runtime_snapshot


def test_pressure_borrow_without_snapshots_is_supported():
    raw = base()
    raw['mechanisms']['shared_memory']['borrow'] = 'pressure'
    spec = ExperimentSpec.model_validate(raw)
    arm = expand_matrix(spec)[0]
    assert arm.resources.memory_scheduling == 'joint'
    assert arm.policy.reclamation.value == 'resident'


def test_borrow_switch_does_not_change_physical_limit():
    raw = base()
    raw['comparison']['arms'] = [dict(name='local'), dict(name='borrow', override={'shared_memory.borrow':'pressure'})]
    spec = ExperimentSpec.model_validate(raw)
    local, borrowed = (effective_spec(spec, p).resources for p in spec.policies)
    assert local.physical_live_limit_mib == borrowed.physical_live_limit_mib
    assert local.shared_memory_borrow_limit_mib is None
    assert borrowed.shared_memory_borrow_limit_mib > 0


def test_physical_limit_checked_independently_from_budget(tmp_path, monkeypatch):
    from clawbox.experiments.memory import CgroupMemorySampler, NodeMemorySampler
    monkeypatch.setattr(NodeMemorySampler, '__init__', lambda *_args, **_kwargs: None)
    (tmp_path/'memory.max').write_text('200')
    sampler = CgroupMemorySampler(tmp_path, capacity_bytes=100, physical_limit_bytes=200)
    assert sampler.total == 100
    with pytest.raises(ValueError, match='memory.max'):
        CgroupMemorySampler(tmp_path, capacity_bytes=100, physical_limit_bytes=201)
    with pytest.raises(ValueError, match='exceeds'):
        CgroupMemorySampler(tmp_path, capacity_bytes=201, physical_limit_bytes=200)


def test_disk_snapshots_keep_live_borrow_pool_but_target_cold():
    raw = base()
    raw['mechanisms']['idle_reclaim']['policy'] = 'wait'
    raw['mechanisms']['shared_memory']['borrow'] = 'pressure'
    spec = ExperimentSpec.model_validate(raw)
    arm = expand_matrix(spec)[0]
    assert arm.resources.warm_memory_capacity_mib > 0
    assert arm.resources.default_snapshot_tier.value == 'cold'


@pytest.mark.parametrize('patch,match', [
    ({'runtime.backend':'agentcgroup', 'runtime.cpu_release_boost': True}, 'unknown mechanism'),
    ({'placement.template_affinity':True}, 'load_aware'),
    ({'idle_reclaim.policy':'release_cost', 'idle_reclaim.scope':'agent_and_tool'}, "Input should be 'off' or 'wait'"),
    ({'coordination.action_selection':'measured_cost'}, 'measured-cost'),
    ({'idle_reclaim.policy':'wait', 'checkpoint.residency':'value'}, 'value residency'),
    ({'checkpoint.lazy_restore':False}, 'unknown mechanism'),
    ({'resources.pool_memory_budget_mib':1}, 'mechanism field'),
    ({'runtime.misspelled':True}, 'unknown mechanism'),
])
def test_invalid_combinations_fail_before_launch(patch, match):
    raw = base()
    raw['comparison']['arms'][0]['override'] = patch
    with pytest.raises(ValueError, match=match):
        ExperimentSpec.model_validate(raw)


def test_physical_limit_uses_node_totals_before_os_placement():
    raw = base()
    r = raw['environment']['resources']
    expected = sum(node['memory_capacity_mib'] for node in r['compute_nodes']) + r['shared_memory_borrow_limit_mib']
    for key in ('local_memory_capacity_mib', 'local_memory_low_watermark_mib', 'local_memory_high_watermark_mib'):
        r.pop(key, None)
    raw['comparison']['arms'] = [dict(name='os', override={'placement.policy':'os_default'}), dict(name='managed')]
    spec = ExperimentSpec.model_validate(raw)
    assert {a.resources.physical_live_limit_mib for a in expand_matrix(spec)} == {expected}
    assert ExperimentSpec.model_validate(export_design(spec)) == spec


@pytest.mark.parametrize('field,value,match', [
    ('local_memory_capacity_mib', 1, 'sum over compute_nodes'),
    ('warm_memory_capacity_mib', 1, '50%'),
])
def test_os_arm_cannot_hide_invalid_environment(field, value, match):
    raw = base()
    raw['environment']['resources'][field] = value
    raw['mechanisms']['placement']['policy'] = 'os_default'
    with pytest.raises(ValueError, match=match):
        ExperimentSpec.model_validate(raw)


@pytest.mark.parametrize('storage', ['disk', 'warm-only', 'tiered'])
def test_resident_checkpoint_settings_are_dormant(storage):
    raw = base()
    raw['mechanisms']['checkpoint'].update(storage=storage, residency='value')
    spec = ExperimentSpec.model_validate(raw)
    arm = expand_matrix(spec)[0]
    assert not arm.policy.snapshot_aware_residency
    assert arm.policy.mechanisms['checkpoint']['storage'] == storage
    assert 'inactive' in design_overview(spec)['arms'][0]['warnings'][0]
    assert ExperimentSpec.model_validate(export_design(spec)) == spec


def test_pressure_borrow_without_reclaim_needs_no_snapshot_paths():
    raw = base()
    raw['mechanisms']['shared_memory']['borrow'] = 'pressure'
    raw['environment']['resources'].update(warm_snapshot_root=None, cold_snapshot_root=None)
    assert expand_matrix(ExperimentSpec.model_validate(raw))[0].resources.memory_scheduling == 'joint'


def test_cpu_boost_rejects_os_placement():
    raw = base()
    raw['mechanisms']['runtime']['cpu_release_boost'] = True
    raw['mechanisms']['placement']['policy'] = 'os_default'
    with pytest.raises(ValueError, match='fixed LOCAL NUMA'):
        ExperimentSpec.model_validate(raw)


def test_per_arm_input_validation_includes_later_predicted_arm(monkeypatch):
    from clawbox.experiments import inputs
    spec = design('admission')
    original = inputs.validate_inputs
    visited = []
    def fake(child):
        if child.schema_version == 4:
            return original(child)
        visited.append(child.policies[0].admission.value)
        return {'traces': [], 'prediction_files': ['prediction.json'] if visited[-1] == 'predicted' else []}
    monkeypatch.setattr(inputs, 'validate_inputs', fake)
    assert fake(spec)['prediction_files'] == ['prediction.json']
    assert visited == ['off', 'fixed', 'predicted']


def test_qualification_covers_distinct_mechanisms_once(tmp_path):
    from clawbox.experiments.qualification import design_probes
    seen = []
    def probe(child, *, artifact_root):
        seen.append(child.resources.snapshot_mechanism)
        return {'passed':True, 'required':True}
    result = design_probes(design('checkpoint', 'encoding'), probe, artifact_root=tmp_path)
    assert seen == ['full-copy', 'incremental-cow']
    assert len(result['probes']) == 2
    seen.clear()
    result = design_probes(design('runtime'), probe, artifact_root=tmp_path)
    assert len(seen) == 1
    assert len(result['by_policy']) == 4


def test_freeze_preserves_qualification_changes():
    spec = design('runtime')
    changed = spec.model_copy(update={
        'experiment_id':'qualification-test',
        'execution':spec.execution.model_copy(update={'concurrency_levels':(3,), 'randomized_order':False}),
        'workload':spec.workload.model_copy(update={'repetitions':1}),
        'validation':spec.validation.model_copy(update={'command':'true'}),
        'inference':spec.inference.model_copy(update={'configuration':{'model':'test','max_model_steps':1}}),
    })
    loaded = ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(changed)))
    assert spec_digest(loaded) == spec_digest(changed)
    assert len(expand_matrix(loaded)) == 4
    assert loaded.execution.concurrency_levels == (3,)


def test_cli_configuration_uses_public_design(tmp_path):
    spec = configure_experiment(Path('examples/experiments/getting-started.yaml'), recipe='runtime',
                                concurrency='1,2', local_numa_node=0,
                                mechanism_settings=['runtime.pressure_control.boost_cpu_weight=250'])
    path = tmp_path/'design.yaml'
    path.write_text(dump_experiment(spec), encoding='utf-8')
    reused = configure_experiment(path, repetitions=3)
    assert reused.schema_version == 4
    assert reused.workload.repetitions == 3
    assert len(reused.policies) == 4
    assert all(p.mechanisms['runtime']['pressure_control']['boost_cpu_weight'] == 250 for p in reused.policies)
    assert design_overview(reused)['arm_count'] == 24

@pytest.mark.parametrize('recipe,axis', [
    ('admission', None), ('runtime', None), ('memory-reclaim', None),
    ('placement', None), ('idle-reclaim', None), ('shared-memory', None),
    ('coordination', None), ('checkpoint', 'encoding'),
    ('checkpoint', 'storage'), ('checkpoint', 'residency'),
])
def test_fixed_methods_reset_inherited_switches_and_roundtrip(recipe, axis):
    from clawbox.experiments.design import fixed_recipe_arms
    raw = base()
    common = raw['mechanisms']
    common['admission']['static_tool_memory_mib'] = 128
    common['runtime'].update(memory_protection=True, cpu_release_boost=True,
                             proactive_memory_reclaim=True)
    common['placement'].update(policy='explicit', template_affinity=True,
                               session_compute_nodes=['unrelated'])
    common['idle_reclaim'].update(policy='wait', scope='agent_and_tool')
    common['shared_memory']['borrow'] = 'pressure'
    common['coordination']['action_selection'] = 'measured_cost'
    common['checkpoint'].update(storage='warm-only', residency='value')
    arms = fixed_recipe_arms(recipe, common=common, checkpoint_axis=axis)
    raw['comparison'] = dict(recipe=recipe, arms=arms)
    spec = ExperimentSpec.model_validate(raw)
    assert ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(spec))) == spec
    for p in spec.policies:
        m = p.mechanisms
        assert m['placement']['session_compute_nodes'] == []
        if recipe != 'runtime':
            assert not m['runtime']['memory_protection']
            assert not m['runtime']['cpu_release_boost']
            assert not p.runtime_control.adaptive
        if recipe != 'memory-reclaim':
            assert not m['runtime']['proactive_memory_reclaim']
        if recipe not in {'coordination', 'shared-memory'}:
            assert m['shared_memory']['borrow'] == 'off'
    # Public method snapshots survive edits to the common switches.
    raw = yaml.safe_load(dump_experiment(spec))
    raw['mechanisms']['runtime']['memory_protection'] = False
    raw['mechanisms']['runtime']['proactive_memory_reclaim'] = False
    again = ExperimentSpec.model_validate(raw)
    assert [p.mechanisms for p in again.policies] == [p.mechanisms for p in spec.policies]


def test_fixed_configure_preconditions(tmp_path):
    path = tmp_path / 'base.yaml'
    raw = base()
    raw['mechanisms']['admission']['static_tool_memory_mib'] = 128
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    spec = configure_experiment(path, recipe='runtime', fixed_methods=True)
    assert all(p.admission.value == 'fixed' for p in spec.policies)
    with pytest.raises(ValueError, match='requires --recipe'):
        configure_experiment(path, fixed_methods=True)
    raw['evidence']['idle_reclaim_costs'] = None
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    assert len(configure_experiment(path, recipe='idle-reclaim', fixed_methods=True).policies) == 3
    with pytest.raises(ValueError, match='single-family'):
        configure_experiment(path, recipe='integrated', fixed_methods=True)
