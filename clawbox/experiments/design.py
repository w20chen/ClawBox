"""Mechanism configuration compiled into the existing internal trial model."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import warnings
from typing import Any, Literal

from pydantic import Field, create_model
import yaml

from .spec import (StrictFrozenModel, ResourcesSpec, ExecutionSpec, SandboxSpec, RuntimeSandboxSpec,
                   WorkloadSpec, AgentSpec, InferenceSpec, ValidationSpec, OutputSpec)

DIMENSIONS = ('admission', 'runtime', 'placement', 'idle_reclaim', 'shared_memory', 'checkpoint')
GROUPS = DIMENSIONS + ('restore', 'deployment', 'coordination')

# Every existing parameter has one public owner. Physical limits stay in the
# environment; decision parameters can vary by arm. No arbitrary resource patch
# is accepted from the public format.
RESOURCE_FIELDS = {
    'admission': ('startup_headroom_mib', 'admission_margin_mib',
                  'static_tool_memory_mib', 'non_command_tool_memory_mib',
                  'prediction_reservation_scale',
                  'small_tool_admission_bypass_mib'),
    'runtime': ('pressure_control', 'runtime_poll_interval_seconds'),
    'placement': (),
    'idle_reclaim': ('checkpoint_restore_headroom_mib',),
    'shared_memory': ('borrow_cooldown_seconds', 'return_cooldown_seconds',
                      'shared_memory_resident_headroom_mib'),
    'checkpoint': ('pool_transfer_concurrency', 'snapshot_transfer_concurrency',
                   'pool_transfer_timeout_seconds'),
}
EVIDENCE_FIELDS = ('prediction_artifact', 'snapshot_residency_costs',
                   'template_base_sharing_artifact')
REMOVED_FIELDS = ('memory_action_costs',)
DERIVED_FIELDS = ('memory_scheduling', 'snapshot_mechanism', 'snapshot_storage',
                  'snapshot_destination', 'template_base_sharing', 'observe_predictions', 'lazy_restore', 'physical_live_limit_mib',
                  'prediction_mode', 'lifecycle_admission', 'proactive_cache_reclaim',
                  'resident_capacity_accounting', 'lifecycle_reservation_scale')
PLACEMENT_FIELDS = (
    'resume_policy', 'migration_threshold_seconds',
    'placement_cpu_overcommit', 'template_balance_slack', 'session_compute_nodes', 'session_cpu_slots',
    'tool_call_cpu_policy', 'cpu_qos_policy', 'cpu_qos_group_idle', 'tool_call_cpu_pressure_ratio',
    'tool_call_cpu_pressure_samples', 'tool_call_cpu_group_max_active',
    'tool_call_cpu_pressure_duration_seconds',
    'tool_call_cpu_min_duration_seconds', 'tool_call_cpu_include_smt',
    'tool_call_cpu_max_utilization', 'tool_call_cpu_poll_interval_seconds',
    'tool_call_cpu_group_order', 'tool_call_cpu_observe_wait',
    'tool_call_cpu_duration_source',
)
MEASUREMENT_FIELDS = ('memory_sample_interval_seconds', 'stabilization_seconds')


def subset(name, source, fields, **extra):
    definitions = {key: (source.model_fields[key].annotation,
                         deepcopy(source.model_fields[key])) for key in source.model_fields if key in fields}
    definitions.update(extra)
    return create_model(name, __base__=StrictFrozenModel, **definitions)


Admission = subset('Admission', ResourcesSpec, RESOURCE_FIELDS['admission'],
                   tool=(Literal['off', 'fixed', 'predicted'], 'off'))
Runtime = subset('Runtime', ResourcesSpec, RESOURCE_FIELDS['runtime'],
                 proactive_memory_reclaim=(bool, False),
                 memory_protection=(bool, False), cpu_release_boost=(bool, False))
Placement = subset('Placement', ExecutionSpec, PLACEMENT_FIELDS,
                   policy=(Literal[
                       'os_default', 'round_robin', 'explicit', 'load_aware',
                       'affinity', 'snapshot_aware',
                   ], 'round_robin'),
                   template_affinity=(bool, False))
IdleReclaim = subset('IdleReclaim', ResourcesSpec, RESOURCE_FIELDS['idle_reclaim'],
                     policy=(Literal['off', 'wait', 'gpu'], 'off'),
                     gpu_reclaim_min_remaining_seconds=(float, Field(default=1.0, ge=0, allow_inf_nan=False)),
                     gpu_signal_max_age_seconds=(float, Field(default=5.0, gt=0, allow_inf_nan=False)),
                     scope=(Literal['tool', 'agent_and_tool'], 'tool'))
SharedMemory = subset('SharedMemory', ResourcesSpec, RESOURCE_FIELDS['shared_memory'],
                      borrow=(Literal['off', 'hard', 'pressure'], 'off'))
Checkpoint = subset('Checkpoint', ResourcesSpec, RESOURCE_FIELDS['checkpoint'],
                    mechanism=(Literal['full-copy', 'incremental-cow'], 'incremental-cow'),
                    storage=(Literal['disk', 'warm-only', 'tiered'], 'disk'),
                    residency=(Literal['lru', 'value', 'next-use'], 'lru'))


class Restore(StrictFrozenModel):
    prewarm: Literal['off', 'eta'] = 'off'
    prewarm_lead_seconds: float = Field(default=2., gt=0, allow_inf_nan=False)
    promotion: Literal['off', 'if_fit'] = 'off'


class Deployment(StrictFrozenModel):
    mode: Literal['managed', 'os-default'] = 'managed'
    resident_capacity_accounting: bool = False
    lifecycle_reservation_scale: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False)


class Coordination(StrictFrozenModel):
    action_selection: Literal['context-first'] = 'context-first'


class Conditions(StrictFrozenModel):
    prediction_source: Literal['kb', 'cold-start-kb', 'host-live', 'command-table'] = 'kb'


class Mechanisms(StrictFrozenModel):
    admission: Admission = Field(default_factory=Admission)
    runtime: Runtime = Field(default_factory=Runtime)
    placement: Placement = Field(default_factory=Placement)
    idle_reclaim: IdleReclaim = Field(default_factory=IdleReclaim)
    shared_memory: SharedMemory = Field(default_factory=SharedMemory)
    checkpoint: Checkpoint = Field(default_factory=Checkpoint)
    restore: Restore = Field(default_factory=Restore)
    deployment: Deployment = Field(default_factory=Deployment)
    coordination: Coordination = Field(default_factory=Coordination)


owned = {key for fields in RESOURCE_FIELDS.values() for key in fields}
EnvironmentResources = subset('EnvironmentResources', ResourcesSpec,
                              set(ResourcesSpec.model_fields) - owned - set(EVIDENCE_FIELDS)
                              - set(REMOVED_FIELDS) - set(DERIVED_FIELDS))
DesignExecution = subset('DesignExecution', ExecutionSpec,
                          set(ExecutionSpec.model_fields) - set(PLACEMENT_FIELDS) - set(MEASUREMENT_FIELDS) - {'placement_policy'})
Evidence = subset('Evidence', ResourcesSpec, EVIDENCE_FIELDS)
Measurement = subset('Measurement', ExecutionSpec, MEASUREMENT_FIELDS,
                     observe_predictions=(bool, False),
                     validation=(ValidationSpec, Field(default_factory=ValidationSpec)))


class Environment(StrictFrozenModel):
    runtime: RuntimeSandboxSpec
    sandbox: SandboxSpec
    resources: EnvironmentResources


class DesignArm(StrictFrozenModel):
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    override: dict[str, Any] = Field(default_factory=dict)


class Comparison(StrictFrozenModel):
    recipe: Literal['admission', 'runtime', 'memory-reclaim', 'placement', 'idle-reclaim', 'shared-memory', 'coordination', 'checkpoint', 'integrated', 'resident-capacity', 'full', 'llc', 'custom'] = 'custom'
    arms: tuple[DesignArm, ...] = Field(min_length=1)


class Design(StrictFrozenModel):
    schema_version: Literal[4] = 4
    experiment_id: str
    environment: Environment
    workload: WorkloadSpec
    agent: AgentSpec = Field(default_factory=AgentSpec)
    inference: InferenceSpec
    evidence: Evidence = Field(default_factory=Evidence)
    measurement: Measurement = Field(default_factory=Measurement)
    mechanisms: Mechanisms = Field(default_factory=Mechanisms)
    conditions: Conditions = Field(default_factory=Conditions)
    comparison: Comparison
    execution: DesignExecution = Field(default_factory=DesignExecution)


def override_mechanisms(common, overrides):
    raw = deepcopy(common)
    for path, value in overrides.items():
        fields = path.split('.')
        if len(fields) < 2 or fields[0] not in GROUPS:
            raise ValueError(f'arm override must name a mechanism field: {path}')
        target = raw
        for field in fields[:-1]:
            if field not in target or not isinstance(target[field], dict):
                raise ValueError(f'unknown mechanism field: {path}')
            target = target[field]
        if fields[-1] not in target:
            raise ValueError(f'unknown mechanism field: {path}')
        target[fields[-1]] = value
    return Mechanisms.model_validate(raw).model_dump(mode='json')


def lower_arm(design, name, m):
    r = design.environment.resources.model_dump(mode='json')
    r['physical_live_limit_mib'] = (r['local_memory_capacity_mib'] or r['pool_memory_budget_mib']) + (r['shared_memory_borrow_limit_mib'] or 0)
    x = design.execution.model_dump(mode='json')
    for dimension, fields in RESOURCE_FIELDS.items():
        r.update({key: m[dimension][key] for key in fields})
    e = design.evidence.model_dump(mode='json')
    r.update({key: e[key] for key in EVIDENCE_FIELDS})
    r['observe_predictions'] = design.measurement.observe_predictions
    x.update({key: getattr(design.measurement, key) for key in MEASUREMENT_FIELDS})
    x.update({key: m['placement'][key] for key in PLACEMENT_FIELDS})
    x['placement_policy'] = m['placement']['policy']
    a, rt, pl, idle, shared, ck = (m[key] for key in DIMENSIONS)
    snapshots = idle['policy'] != 'off'
    coordination = m['coordination']
    os_deployment = m['deployment']['mode'] == 'os-default'
    if m['restore']['prewarm'] == 'eta' and not snapshots:
        raise ValueError(f'{name}: ETA prewarm requires idle reclaim; resident VMs need no restore')
    if pl['cpu_qos_group_idle'] and pl['cpu_qos_policy'] == 'off':
        raise ValueError(f'{name}: cpu_qos_group_idle requires idle or idle_core CPU QoS')
    if pl['cpu_qos_policy'] != 'off' and (
            os_deployment or pl['policy'] == 'os_default' or not r['local_memory_cgroup']):
        raise ValueError(
            f'{name}: CPU QoS requires managed placement and the verified local_memory_cgroup path'
        )
    if pl['tool_call_cpu_policy'] != 'c0':
        if not r['compute_nodes'] or not r['local_memory_cgroup']:
            raise ValueError(
                f'{name}: LLC Tool-call CPU placement requires compute_nodes and '
                'the verified local_memory_cgroup path'
            )
        if pl['policy'] == 'os_default':
            raise ValueError(f'{name}: LLC Tool-call CPU placement requires managed NUMA placement')
    if os_deployment and (pl['policy'] != 'os_default' or a['tool'] != 'off'
            or snapshots or shared['borrow'] != 'off' or pl['template_affinity']
            or rt['memory_protection'] or rt['cpu_release_boost'] or rt['proactive_memory_reclaim']):
        raise ValueError(f'{name}: os-default deployment requires OS placement, no tool admission, resident VMs and all runtime controls/borrowing/affinity off')
    r['lifecycle_admission'] = 'os-managed' if os_deployment else 'configured-capacity'
    r['resident_capacity_accounting'] = m['deployment']['resident_capacity_accounting']
    r['lifecycle_reservation_scale'] = m['deployment']['lifecycle_reservation_scale']
    if r['resident_capacity_accounting'] and (os_deployment or snapshots):
        raise ValueError('resident capacity accounting requires managed resident VMs')
    if r['lifecycle_reservation_scale'] != 1 and not r['resident_capacity_accounting']:
        raise ValueError('discounted lifecycle reservations require resident capacity accounting')
    r['prediction_mode'] = {
        'kb': 'runtime', 'cold-start-kb': 'cold-start', 'host-live': 'host-live', 'command-table': 'frozen',
    }[design.conditions.prediction_source]
    r['proactive_cache_reclaim'] = rt['proactive_memory_reclaim']
    if r['resident_capacity_accounting']:
        # The static ledger uses allocated LOCAL capacity, not pressure thresholds.
        r['pool_memory_budget_mib'] = r['local_memory_capacity_mib'] or r['pool_memory_budget_mib']
    if rt['cpu_release_boost'] and (pl['policy'] == 'os_default' or
            (not r['compute_nodes'] and r['local_numa_node'] is None)):
        raise ValueError(f'{name}: cpu_release_boost requires fixed LOCAL NUMA placement; configure compute_nodes or local_numa_node and use managed placement')
    if shared['borrow'] != 'off' and not r['shared_memory_borrow_limit_mib']:
        raise ValueError(f'{name}: borrowing requires a positive environment shared_memory_borrow_limit_mib')
    if snapshots and ck['residency'] in {'value', 'next-use'} and ck['storage'] != 'tiered':
        raise ValueError(
            f"{name}: {ck['residency']} residency requires idle reclaim and tiered storage"
        )
    if snapshots and ck['residency'] == 'next-use' and (
            design.inference.backend.value != 'serving_sim'):
        raise ValueError(
            f'{name}: next-use residency requires serving_sim signals and idle reclaim'
        )
    if snapshots and ck['storage'] != 'disk' and (not r['warm_memory_capacity_mib'] or not r['warm_snapshot_root']):
        raise ValueError(f'{name}: WARM storage requires configured pool capacity and path')
    peer_storage = (r.get('snapshot_scheduling_storage') or {}).get('topology') == 'peer-local-ssd'
    if pl['resume_policy'] != 'home' and (
            not snapshots or idle['scope'] != 'agent_and_tool' or os_deployment or not peer_storage):
        raise ValueError(f'{name}: non-home resume requires peer-local-ssd, managed deployment and agent_and_tool idle reclaim')
    if snapshots and ck['storage'] in {'disk', 'tiered'} and not r['cold_snapshot_root'] and not peer_storage:
        raise ValueError(f'{name}: disk/tiered snapshots require cold_snapshot_root')
    if shared['borrow'] == 'off':
        r['shared_memory_borrow_limit_mib'] = None
        r['shared_memory_resident_headroom_mib'] = None
    # Borrowing and idle checkpointing serve different execution contexts.
    # Do not flatten them into a synthetic seconds/GiB comparison.
    r['memory_action_costs'] = None
    r['memory_scheduling'] = ('joint' if shared['borrow'] == 'pressure' else
                              'static' if shared['borrow'] == 'hard' else
                              'snapshot_only' if snapshots else 'local_only')
    r['template_base_sharing'] = 'required' if pl['template_affinity'] else 'disabled'
    r['snapshot_mechanism'] = ck['mechanism']
    # This internal flag retains WARM accounting; it is not a backend loading switch.
    r['lazy_restore'] = True
    r['snapshot_storage'] = 'warm-only' if ck['storage'] == 'warm-only' else 'tiered'
    r['snapshot_destination'] = 'cold' if ck['storage'] == 'disk' else 'warm'
    if not snapshots:
        # Preserve declared checkpoint settings in mechanisms, but do not let
        # unused storage/encoding prerequisites constrain a resident control.
        r.update(snapshot_storage='tiered', snapshot_destination='cold')
    # Inactive operations do not remove the common physical shared-pool
    # boundary. Existing charged shared-node pages can outlive a prior VM;
    # treating the pool as zero makes a resident arm fail on those pages.
    if pl['policy'] == 'os_default':
        if (shared['borrow'] != 'off' or pl['template_affinity']
                or pl['tool_call_cpu_policy'] != 'c0'):
            raise ValueError(
                f'{name}: OS placement cannot enforce per-node borrowing, template affinity, '
                'or topology CPU placement'
            )
        # OS baseline inherits exactly the same host live-memory boundary.
        env = design.environment.resources
        capacity = (env.local_memory_capacity_mib or env.pool_memory_budget_mib) + (env.shared_memory_borrow_limit_mib or 0)
        r.update(compute_nodes=[], local_numa_node=None, local_memory_capacity_mib=capacity,
                 pool_memory_budget_mib=capacity, local_memory_low_watermark_mib=None,
                 local_memory_high_watermark_mib=None)
    adaptive = rt['memory_protection'] or rt['cpu_release_boost']
    p = dict(name=name, deployment=m['deployment']['mode'], admission=a['tool'],
             runtime_control='explicit' if adaptive else 'off',
             memory_protection=rt['memory_protection'], cpu_release_boost=rt['cpu_release_boost'],
             idle_release_ranking=False, runtime_snapshot=idle['scope'] == 'agent_and_tool',
             reclamation='snapshot_pause' if snapshots else 'resident',
             eviction='gpu_aware_pressure' if idle['policy'] == 'gpu' else 'wait_aware_pressure' if snapshots else 'none',
             restore='reactive' if snapshots else 'none',
             gpu_reclaim_min_remaining_seconds=idle['gpu_reclaim_min_remaining_seconds'],
             gpu_signal_max_age_seconds=idle['gpu_signal_max_age_seconds'],
             gpu_prewarm=m['restore']['prewarm'],
             gpu_prewarm_lead_seconds=m['restore']['prewarm_lead_seconds'],
             gpu_signals=design.execution.snapshot_page_management is not None,
             template_aware_placement=pl['template_affinity'],
             snapshot_aware_residency=snapshots and ck['residency'] == 'value',
             snapshot_next_use_residency=snapshots and ck['residency'] == 'next-use',
             snapshot_restore_promotion=m['restore']['promotion'],
             mechanisms=m, resource_overrides=r,
             execution_overrides={key: x[key] for key in (*PLACEMENT_FIELDS, 'placement_policy')})
    return p, r, x


def compile_design(raw):
    d = Design.model_validate(raw)
    # Validate the physical environment before any arm disables borrowing or
    # removes NUMA placement. Also fill aggregate capacities from compute_nodes
    # before calculating the common physical cgroup limit.
    physical = ResourcesSpec.model_validate(d.environment.resources.model_dump(mode='json'))
    environment_resources = EnvironmentResources.model_validate({
        key: getattr(physical, key) for key in EnvironmentResources.model_fields})
    d = d.model_copy(update={'environment': d.environment.model_copy(
        update={'resources': environment_resources})})
    common = d.mechanisms.model_dump(mode='json')
    policies = []
    resources = execution = None
    for arm in d.comparison.arms:
        p, r, x = lower_arm(d, arm.name, override_mechanisms(common, arm.override))
        policies.append(p)
        if resources is None:
            resources, execution = r, x
    inference = d.inference.model_dump(mode='json')
    frozen = d.model_dump(mode='json')
    frozen['comparison']['arms'] = [dict(name=p['name'], override={
        f'{dimension}.{key}': value for dimension in GROUPS
        for key, value in p['mechanisms'][dimension].items()
        if common[dimension][key] != value or f'{dimension}.{key}' in arm.override})
        for p, arm in zip(policies, d.comparison.arms)]
    return dict(schema_version=4, design=frozen, experiment_id=d.experiment_id,
                workload=d.workload.model_dump(mode='json'), agent=d.agent.model_dump(mode='json'),
                inference=inference, runtime=d.environment.runtime.model_dump(mode='json'),
                sandbox=d.environment.sandbox.model_dump(mode='json'), resources=resources,
                execution=execution, policies=policies,
                validation=d.measurement.validation.model_dump(mode='json'),
                output=OutputSpec().model_dump(mode='json'))


def export_design(spec):
    """Freeze explicit effective arms, including qualification workload edits."""
    raw = deepcopy(spec.design)
    raw['experiment_id'] = spec.experiment_id
    raw['workload'] = spec.workload.model_dump(mode='json')
    raw['inference'] = spec.inference.model_dump(mode='json')
    raw['execution'] = {key: getattr(spec.execution, key) for key in DesignExecution.model_fields}
    raw['measurement'].update({key: getattr(spec.execution, key) for key in MEASUREMENT_FIELDS})
    raw['measurement']['validation'] = spec.validation.model_dump(mode='json')
    common = raw['mechanisms']
    declared = {arm['name']: arm.get('override', {}) for arm in raw['comparison']['arms']}
    raw['comparison']['arms'] = [dict(name=p.name, override={
        f'{dimension}.{key}': value for dimension in GROUPS
        for key, value in p.mechanisms[dimension].items()
        if common[dimension][key] != value or f'{dimension}.{key}' in declared.get(p.name, {})}) for p in spec.policies]
    return Design.model_validate(raw).model_dump(mode='json')


def design_overview(spec):
    from .spec import load_workload_cases
    from .spec import expand_matrix, effective_spec
    rows = []
    common = spec.design['mechanisms']
    for p in spec.policies:
        resolved = effective_spec(spec, p)
        requirements = ['cgroup v2 delegation and verified preallocation routing']
        warnings = []
        if spec.design['conditions']['prediction_source'] == 'command-table':
            warnings.append('Diagnostic command-table prediction source; not the KB prediction experiment.')
        if p.reclamation.value == 'resident':
            warnings.append('Checkpoint encoding, storage and residency are inactive while idle_reclaim.policy=off; pool transfer limits still apply to live borrowing.')
        if p.admission.value == 'predicted':
            requirements.append('validated independent prediction artifact matching the Tool environment')
        if p.runtime_control.adaptive:
            requirements.append('progress-enabled Toolbridge; relevant PSI and measured release history for CPU boost')
        if p.reclamation.value == 'snapshot_pause':
            requirements.append('snapshot/restore qualification; actual outstanding model requests')
        if p.template_aware_placement:
            requirements.append('physical template-base sharing qualification')
        if p.snapshot_restore_promotion == 'if_fit':
            requirements.append('qualified cross-tier snapshot relocation for restore promotion')
        if p.snapshot_next_use_residency:
            requirements.append(
                'serving_sim response_forecast feed with expiry; online GPU signals; idle reclaim; '
                'qualified cross-tier snapshot relocation'
            )
            warnings.append(
                'next-use uses expiring native-response ETA with recorded current-call output length; excludes HTTP and VM restore.'
            )
        costs = resolved.resources.snapshot_residency_costs
        if p.snapshot_aware_residency and costs and costs.cold_restore_seconds_per_gib <= costs.warm_restore_seconds_per_gib:
            warnings.append('Measured COLD restore is no slower than WARM: retention value is zero.')
        rows.append(dict(name=p.name, mechanisms=p.mechanisms, requirements=requirements, warnings=warnings,
                         differences={f'{d}.{k}': v for d in GROUPS for k, v in p.mechanisms[d].items()
                                      if common[d][k] != v},
                         effective_resources=resolved.resources.model_dump(mode='json'),
                         effective_placement=resolved.execution.placement_policy,
                         effective_resume_policy=resolved.execution.resume_policy))
    inference = spec.inference.model_dump(mode='json')
    simulator = None
    if 'serving_sim_launch' in spec.inference.configuration:
        from .spec import ServingSimLaunchSpec
        from .serving_sim_process import serving_sim_cluster_layouts
        launch = ServingSimLaunchSpec.model_validate(spec.inference.configuration['serving_sim_launch'])
        layouts = serving_sim_cluster_layouts(spec.inference.configuration, launch, Path('.'))
        simulator = dict(
            clusters=[{key: item[key] for key in ('cluster_id', 'port', 'cpu_affinity', 'cores')}
                      for item in layouts],
            configured_cpu_pool=list(launch.cpu_affinity),
            active_cpu_ids=sorted({cpu for item in layouts for cpu in item['cpu_affinity']}),
            shared_cpus=launch.cluster_count > len(launch.cpu_affinity),
            persistence_semantics='native incremental input flushing and final summaries at graceful shutdown',
        )
    return dict(experiment_id=spec.experiment_id, recipe=spec.design['comparison']['recipe'],
                dimensions=list(DIMENSIONS), control_groups=['deployment', 'coordination'],
                conditions=spec.design['conditions'], arms=rows, arm_count=len(expand_matrix(spec)),
                environment=spec.design['environment'], evidence=spec.design['evidence'],
                workload={'session_assignment': spec.workload.session_assignment.value,
                          'base_case_count': len(load_workload_cases(spec.workload)),
                          'rollouts_per_case': spec.workload.rollouts_per_case,
                          'offered_sessions': list(spec.execution.concurrency_levels),
                          'offered_vms': [2*count for count in spec.execution.concurrency_levels]},
                execution=spec.design['execution'], inference=inference, simulator=simulator,
                configuration_ownership=dict(
                    target_node=spec.resources.target_node,
                    output_root='CLI --output-root > CLAWBOX_OUTPUT_ROOT > results',
                    tool_network=dict(owner='environment.sandbox.allow_internet_access',
                                      allow_internet_access=spec.sandbox.allow_internet_access),
                    fixed=dict(kb_scope_policy=spec.execution.kb_scope_policy,
                               action_selection=common['coordination']['action_selection']),
                ))
