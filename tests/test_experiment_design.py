from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from clawbox.experiments.configuration import dump_experiment
from clawbox.experiments.design import (DIMENSIONS, GROUPS, Design, Mechanisms, compile_design,
                                       export_design, design_overview)
from clawbox.experiments.spec import ExperimentSpec, effective_spec, expand_matrix, spec_digest










def test_peer_resume_policies_roundtrip_without_branching_or_global_cold_root():
    raw = base()
    resources = raw['environment']['resources']
    resources['cold_snapshot_root'] = None
    resources['snapshot_scheduling_storage'] = dict(
        topology='peer-local-ssd', transport='peer-network',
        local_roots={'node0': '/peer/n0', 'node1': '/peer/n1'},
        local_capacity_mib_by_node={'node0': 16384, 'node1': 16384},
        transfer_endpoints={n: dict(netns='peer-'+n, address=a) for n, a in
                            [('node0', '10.240.0.1'), ('node1', '10.240.0.2')]},
        warm_root=resources['warm_snapshot_root'],
        warm_capacity_mib=resources['warm_memory_capacity_mib'],
        host_receipt='/peer/receipt.json',
        native_control_socket='/peer/native.sock',
        restore_costs=dict(provenance='unit-test-only', effective_transfer_mib_per_second=100,
                           import_seconds=.1, native_restore_seconds=.2, resource_wait_seconds=1),
    )
    raw['mechanisms']['idle_reclaim'].update(policy='wait', scope='agent_and_tool')
    raw['comparison']['arms'] = [dict(name=p, override={'placement.resume_policy': p})
                                for p in ('home', 'load-aware', 'cost-aware')]
    spec = ExperimentSpec.model_validate(raw)
    assert {a.execution.resume_policy for a in expand_matrix(spec)} == {'home', 'load-aware', 'cost-aware'}
    assert all(a.execution.snapshot_branching is None for a in expand_matrix(spec))
    assert ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(spec))['specification']) == spec
    raw['mechanisms']['idle_reclaim']['scope'] = 'tool'
    with pytest.raises(ValueError, match='agent_and_tool'):
        ExperimentSpec.model_validate(raw)


def test_session_cpu_slots_roundtrip_bound_workers_and_reject_overlap():
    from clawbox.experiments.worker import session_worker_count
    raw = base()
    raw['mechanisms']['placement']['session_cpu_slots'] = [
        {'node_id': 'node0', 'cpus': '0,2,4,6'}]
    spec = ExperimentSpec.model_validate(raw)
    assert ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(spec))['specification']) == spec
    assert session_worker_count(expand_matrix(spec)[0]) == 1
    raw['mechanisms']['placement']['session_cpu_slots'].append(
        {'node_id': 'node0', 'cpus': '4,8'})
    with pytest.raises(ValueError, match='must not overlap'):
        ExperimentSpec.model_validate(raw)


def test_cpu_group_idle_requires_enabled_qos_and_survives_export():
    raw = base()
    raw['mechanisms']['placement']['cpu_qos_group_idle'] = True
    with pytest.raises(ValueError, match='requires idle or idle_core'):
        ExperimentSpec.model_validate(raw)
    raw['mechanisms']['placement']['cpu_qos_policy'] = 'idle_core'
    spec = ExperimentSpec.model_validate(raw)
    assert expand_matrix(spec)[0].execution.cpu_qos_group_idle is True
    reloaded = ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(spec))['specification'])
    assert expand_matrix(reloaded)[0].execution.cpu_qos_group_idle is True




def test_idle_qos_rejects_unmanaged_placement():
    raw = base()
    raw['mechanisms']['placement'].update(policy='os_default', cpu_qos_policy='idle')
    with pytest.raises(ValueError, match='CPU QoS requires managed placement'):
        ExperimentSpec.model_validate(raw)


def test_explicit_session_arrivals_roundtrip_and_validate():
    raw = base()
    raw['execution']['session_arrival_offsets_seconds'] = [1.25, 3.5]
    spec = ExperimentSpec.model_validate(raw)
    assert spec.execution.session_arrival_offsets_seconds == (1.25, 3.5)
    assert ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(spec))['specification']) == spec
    for offsets in ([0.0], [-1.0, 2.0], [1.0, float('nan')], [1.0, float('inf')], [2.0, 1.0]):
        raw['execution']['session_arrival_offsets_seconds'] = offsets
        with pytest.raises(ValueError, match='session_arrival_offsets_seconds'):
            ExperimentSpec.model_validate(raw)
    raw['execution'].update(concurrency_levels=[1, 2], session_arrival_offsets_seconds=[1.25, 3.5])
    with pytest.raises(ValueError, match='exactly one concurrency level'):
        ExperimentSpec.model_validate(raw)




def test_host_live_options_roundtrip_and_reject_invalid_scope():
    raw = base()
    raw['conditions'] = {'prediction_source': 'host-live'}
    raw['execution'].update(kb_scope_policy='pair', retain_clause_telemetry_artifacts=False,
                            kb_http_timeout_seconds=12.5, kb_observation_retries=3)
    spec = ExperimentSpec.model_validate(raw)
    loaded = ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(spec))['specification'])
    assert loaded == spec
    assert loaded.resources.prediction_mode == 'host-live'
    assert loaded.execution.kb_http_timeout_seconds == 12.5
    raw['execution']['kb_scope_policy'] = 'shared'
    with pytest.raises(ValueError):
        ExperimentSpec.model_validate(raw)


def test_resident_warm_headroom_is_public_and_disabled_with_borrowing():
    raw = base()
    raw['mechanisms']['shared_memory'].update(
        borrow='pressure', shared_memory_resident_headroom_mib=8192,
    )
    raw['mechanisms']['idle_reclaim']['policy'] = 'wait'
    raw['mechanisms']['checkpoint']['storage'] = 'tiered'
    raw['comparison']['arms'] = [
        {'name': 'resident-warm'},
        {'name': 'no-borrow', 'override': {'shared_memory.borrow': 'off'}},
    ]
    spec = ExperimentSpec.model_validate(raw)
    arms = {arm.policy.name: arm for arm in expand_matrix(spec)}
    assert arms['resident-warm'].resources.shared_memory_resident_headroom_mib == 8192
    assert arms['no-borrow'].resources.shared_memory_resident_headroom_mib is None




@pytest.mark.parametrize('configured, expected', [(None, 4), (0, 0), (16, 16)])
def test_small_tool_admission_bypass_is_an_editable_admission_field(configured, expected):
    raw = base()
    if configured is not None:
        raw['mechanisms']['admission']['small_tool_admission_bypass_mib'] = configured

    spec = ExperimentSpec.model_validate(raw)
    arm = expand_matrix(spec)[0]

    assert arm.resources.small_tool_admission_bypass_mib == expected
    assert ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(spec))['specification']) == spec


@pytest.mark.parametrize('mode', ['c2', 'b'])
@pytest.mark.parametrize('include_smt', [None, True, False])
@pytest.mark.parametrize('source', [None, 'tool', 'lattice'])
def test_llc_tool_call_cpu_policy_roundtrips_and_rejects_os_placement(include_smt, mode, source):
    raw = base()
    raw['mechanisms']['placement'].update(
        tool_call_cpu_policy=mode, tool_call_cpu_pressure_ratio=.2,
        tool_call_cpu_pressure_samples=3, tool_call_cpu_group_max_active=3,
        tool_call_cpu_min_duration_seconds=1.5,
    )
    if include_smt is not None:
        raw['mechanisms']['placement']['tool_call_cpu_include_smt'] = include_smt
    if source is not None:
        raw['mechanisms']['placement']['tool_call_cpu_duration_source'] = source
    spec = ExperimentSpec.model_validate(raw)
    arm = expand_matrix(spec)[0]
    assert arm.execution.tool_call_cpu_policy == mode
    assert arm.execution.tool_call_cpu_pressure_ratio == .2
    assert arm.execution.tool_call_cpu_group_max_active == 3
    assert arm.execution.tool_call_cpu_min_duration_seconds == 1.5
    assert arm.execution.tool_call_cpu_duration_source == (source or 'tool')
    assert arm.execution.tool_call_cpu_include_smt is (True if include_smt is None else include_smt)
    assert ExperimentSpec.model_validate(yaml.safe_load(dump_experiment(spec))['specification']) == spec

    raw['mechanisms']['placement']['policy'] = 'os_default'
    with pytest.raises(ValueError, match='LLC Tool-call CPU placement'):
        ExperimentSpec.model_validate(raw)


def test_removed_c2_cooldown_field_is_rejected():
    raw = base()
    raw['mechanisms']['placement']['tool_call_cpu_cooldown_seconds'] = 3
    with pytest.raises(ValueError, match='Extra inputs are not permitted'):
        ExperimentSpec.model_validate(raw)


def test_invalid_b_duration_source_is_rejected():
    raw = base()
    raw['mechanisms']['placement']['tool_call_cpu_duration_source'] = 'trie'
    with pytest.raises(ValueError, match='tool_call_cpu_duration_source'):
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


@pytest.mark.parametrize('switch,value', [('idle_reclaim.policy', 'off'), ('shared_memory.borrow', 'off')])
def test_full_single_capability_ablation_retains_context_coordination(switch, value):
    raw = base()
    raw['mechanisms']['placement']['policy'] = 'load_aware'
    raw['mechanisms']['runtime'].update(memory_protection=True, cpu_release_boost=True)
    raw['mechanisms']['admission']['tool'] = 'predicted'
    raw['mechanisms']['shared_memory']['borrow'] = 'pressure'
    raw['mechanisms']['idle_reclaim']['policy'] = 'wait'
    raw['mechanisms']['checkpoint']['storage'] = 'tiered'
    raw['mechanisms']['coordination']['action_selection'] = 'context-first'
    raw['comparison']['arms'] = [{'name': 'full'}, {'name': 'ablation', 'override': {switch: value}}]
    spec = ExperimentSpec.model_validate(raw)
    full, ablation = spec.policies
    differences = {f'{group}.{key}' for group in GROUPS for key in full.mechanisms[group]
                   if full.mechanisms[group][key] != ablation.mechanisms[group][key]}
    assert differences == {switch}
    assert ablation.mechanisms['coordination']['action_selection'] == 'context-first'
    assert effective_spec(spec, full).resources.physical_live_limit_mib == effective_spec(spec, ablation).resources.physical_live_limit_mib
    assert effective_spec(spec, ablation).resources.memory_action_costs is None




def test_wait_checkpoint_does_not_require_llm_duration_evidence():
    raw = base()
    raw['mechanisms']['idle_reclaim'].update(policy='wait', scope='agent_and_tool')
    spec = ExperimentSpec.model_validate(raw)
    assert spec.policies[0].runtime_snapshot
    assert spec.inference.configuration.get('model_wait_prediction_seconds') is None

    raw['mechanisms']['idle_reclaim'].update(policy='release_cost', scope='tool')
    with pytest.raises(ValueError, match="Input should be 'off', 'wait' or 'gpu'"):
        ExperimentSpec.model_validate(raw)


def test_resident_arm_keeps_common_physical_shared_pool_capacity():
    raw = base()
    raw['mechanisms']['runtime'].update(memory_protection=True, cpu_release_boost=True)
    spec = ExperimentSpec.model_validate(raw)
    arm = expand_matrix(spec)[0]
    assert arm.resources.warm_memory_capacity_mib == raw['environment']['resources']['warm_memory_capacity_mib']
    assert not arm.resources.shared_memory_borrow_limit_mib
    assert arm.policy.reclamation.value == 'resident'






def test_os_only_design_checks_common_physical_shared_pool(monkeypatch):
    from clawbox.experiments import base_cache
    monkeypatch.setattr(base_cache, 'check_experiment', lambda _: {'enabled': False})
    from clawbox.experiments import qualification
    monkeypatch.setattr(qualification, 'require_empty_vm_domain', lambda _: None)
    raw = base()
    raw['mechanisms'].update(
        deployment={'mode': 'os-default'},
        coordination={'action_selection': 'context-first'},
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

    qualification.require_host_ready(spec, launch_checks=False)

    assert profiles[0]['shared_memory_borrow_limit_mib'] == \
        raw['environment']['resources']['shared_memory_borrow_limit_mib']












@pytest.mark.parametrize('field,value', [
    ('idle_reclaim.restore', 'reactive'), ('placement.template_base_sharing', 'required'),
    ('checkpoint.lazy_restore', False), ('admission.prediction_mode', 'frozen'),
    ('admission.lifecycle_admission', 'os-managed'), ('shared_memory.action_selection', 'context-first'),
    ('runtime.proactive_cache_reclaim', True),
])
def test_removed_or_reclassified_fields_are_not_silently_accepted(field, value):
    raw = base()
    raw['comparison']['arms'][0].setdefault('override', {})[field] = value
    with pytest.raises(ValueError, match='unknown mechanism field'):
        ExperimentSpec.model_validate(raw)














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
    ({'idle_reclaim.policy':'release_cost', 'idle_reclaim.scope':'agent_and_tool'}, "Input should be 'off', 'wait' or 'gpu'"),
    ({'coordination.action_selection':'measured_cost'}, 'context-first'),
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
    ('warm_memory_capacity_mib', 1, 'live borrow limit'),
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















@pytest.mark.parametrize('scale', [0, -1, 1.1, float('nan'), float('inf')])
def test_resident_capacity_rejects_invalid_scales(scale):
    raw = base()
    raw['mechanisms']['deployment'].update(resident_capacity_accounting=True, lifecycle_reservation_scale=scale)
    with pytest.raises(ValueError):
        ExperimentSpec.model_validate(raw)


@pytest.mark.parametrize('change', ['snapshot', 'os-default', 'missing-accounting'])
def test_resident_capacity_rejects_incompatible_modes(change):
    raw = base()
    raw['mechanisms']['deployment'].update(resident_capacity_accounting=True, lifecycle_reservation_scale=.8)
    if change == 'snapshot':
        raw['mechanisms']['idle_reclaim']['policy'] = 'wait'
    elif change == 'os-default':
        raw['mechanisms']['deployment']['mode'] = 'os-default'
        raw['mechanisms']['placement']['policy'] = 'os_default'
    else:
        raw['mechanisms']['deployment']['resident_capacity_accounting'] = False
    with pytest.raises(ValueError):
        ExperimentSpec.model_validate(raw)


@pytest.mark.parametrize('group,updates,error', [
    ('admission', {'tool': 'fixed', 'static_tool_memory_mib': 128}, 'tool admission=off'),
    ('admission', {'tool': 'predicted'}, 'tool admission=off'),
    ('shared_memory', {'borrow': 'hard'}, 'shared-memory borrowing'),
    ('shared_memory', {'borrow': 'pressure'}, 'shared-memory borrowing'),
    ('runtime', {'proactive_memory_reclaim': True}, 'proactive memory reclaim'),
])
def test_pure_ledger_rejects_unsupported_mechanism_combinations(group, updates, error):
    raw = base()
    raw['mechanisms']['deployment']['resident_capacity_accounting'] = True
    raw['mechanisms'][group].update(updates)
    with pytest.raises(ValueError, match=error):
        ExperimentSpec.model_validate(raw)
    # The same mechanisms remain available under the original measured mode.
    raw['mechanisms']['deployment']['resident_capacity_accounting'] = False
    ExperimentSpec.model_validate(raw)


















def test_explicit_runtime_shape_survives_runtime_model_conversion():
    from clawbox.experiments.spec import SandboxSpec, RuntimeSandboxSpec
    assert RuntimeSandboxSpec(template_id='runtime').memory_mib == 1024
    assert SandboxSpec(template_id='tool').memory_mib == 3072
    old = SandboxSpec(template_id='historical-runtime', memory_mib=2048)
    assert RuntimeSandboxSpec.model_validate(old).memory_mib == 2048








def base():
    raw = yaml.safe_load(Path('tests/fixtures/experiments/agent-rl-heterogeneous-ablation-kunpeng.yaml').read_text(encoding='utf-8'))
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
                        cold_restore_seconds_per_gib=2)),
                execution=dict(concurrency_levels=[2], randomized_order=False),
                comparison=dict(recipe='custom', arms=[dict(name='custom')]))
