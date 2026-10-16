from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from clawbox.experiments.configure import configure_experiment, dump_experiment
from clawbox.experiments.design import (DIMENSIONS, Design, Mechanisms, compile_design,
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
                    model_wait_prediction_seconds=30, model_wait_prediction_source='separate-training',
                    snapshot_residency_costs=dict(provenance='unit-test', warm_restore_seconds_per_gib=1, cold_restore_seconds_per_gib=2),
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
    if recipe in {'shared-memory', 'checkpoint', 'integrated'}:
        m['idle_reclaim']['policy'] = 'wait'
        m['checkpoint']['storage'] = 'tiered'
    if recipe == 'integrated':
        m['placement'].update(policy='load_aware', template_affinity=True, template_base_sharing='required')
        m['runtime'].update(memory_protection=True, cpu_release_boost=True)
        m['shared_memory']['borrow'] = 'pressure'
        m['checkpoint']['residency'] = 'value'
    raw['comparison'] = dict(recipe=recipe, arms=recipe_arms(recipe, checkpoint_axis=axis))
    return ExperimentSpec.model_validate(raw)


@pytest.mark.parametrize('recipe,axis,count', [
    ('admission', None, 3), ('runtime', None, 5), ('placement', None, 3),
    ('idle-reclaim', None, 4), ('shared-memory', None, 4),
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
    assert all(set(p.mechanisms) == set(DIMENSIONS) for p in spec.policies)


def test_recipe_and_arm_names_do_not_change_execution():
    raw = export_design(design('runtime'))
    before = expand_matrix(ExperimentSpec.model_validate(raw))
    raw['comparison']['recipe'] = 'admission'
    for i, p in enumerate(raw['comparison']['arms']):
        p['name'] = 'renamed-'+str(i)
    after = expand_matrix(ExperimentSpec.model_validate(raw))
    assert [(a.resources, a.execution, a.policy.mechanisms) for a in before] == [
        (a.resources, a.execution, a.policy.mechanisms) for a in after]


def test_runtime_controls_do_not_enable_reclaim():
    spec = design('runtime')
    p = spec.policies[-1]
    assert p.memory_protection and p.cpu_release_boost
    assert not p.coordinates_release
    assert p.reclamation.value == 'resident'
    assert p.runtime_control.value == 'explicit'


def test_reclaim_ranking_does_not_enable_cpu_or_memory_controls():
    p = design('idle-reclaim').policies[-1]
    assert p.coordinates_release
    assert not p.memory_protection and not p.cpu_release_boost
    assert not p.runtime_snapshot


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
    ({'runtime.backend':'agentcgroup', 'runtime.cpu_release_boost': True}, 'AgentCgroup'),
    ({'placement.template_affinity':True}, 'verified'),
    ({'idle_reclaim.policy':'release_cost', 'idle_reclaim.scope':'agent_and_tool'}, 'Tool eviction only'),
    ({'shared_memory.action_selection':'measured_cost'}, 'measured-cost'),
    ({'idle_reclaim.policy':'wait', 'checkpoint.residency':'value'}, 'value residency'),
    ({'idle_reclaim.policy':'wait', 'checkpoint.lazy_restore':False}, 'lazy_restore'),
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
    raw['mechanisms']['checkpoint'].update(storage=storage, residency='value', lazy_restore=False)
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
    assert len(result['by_policy']) == 5


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
    assert len(expand_matrix(loaded)) == 5
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
    assert len(reused.policies) == 5
    assert all(p.mechanisms['runtime']['pressure_control']['boost_cpu_weight'] == 250 for p in reused.policies)
    assert design_overview(reused)['arm_count'] == 30
