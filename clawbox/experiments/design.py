"""Public six-dimension designs compiled into the existing experiment worker.

Recipes only generate editable arms. Neither recipe names nor arm names select
runtime behavior. Each arm is validated with its own resolved resources.
"""
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
GROUPS = DIMENSIONS + ('deployment', 'coordination')
RECIPES = ('admission', 'runtime', 'memory-reclaim', 'placement', 'idle-reclaim', 'shared-memory', 'coordination',
           'checkpoint', 'integrated', 'resident-capacity', 'custom')

# Every existing parameter has one public owner. Physical limits stay in the
# environment; decision parameters can vary by arm. No arbitrary resource patch
# is accepted from the public format.
RESOURCE_FIELDS = {
    'admission': ('startup_headroom_mib', 'admission_margin_mib',
                  'static_tool_memory_mib', 'non_command_tool_memory_mib',
                  'prediction_reservation_scale'),
    'runtime': ('pressure_control', 'runtime_poll_interval_seconds'),
    'placement': (),
    'idle_reclaim': ('checkpoint_restore_headroom_mib',),
    'shared_memory': ('borrow_cooldown_seconds', 'return_cooldown_seconds'),
    'checkpoint': ('pool_transfer_concurrency', 'pool_transfer_timeout_seconds'),
}
EVIDENCE_FIELDS = ('prediction_artifact', 'memory_action_costs', 'snapshot_residency_costs', 'idle_reclaim_costs')
DERIVED_FIELDS = ('memory_scheduling', 'snapshot_mechanism', 'snapshot_storage',
                  'snapshot_destination', 'template_base_sharing', 'observe_predictions', 'lazy_restore', 'physical_live_limit_mib',
                  'prediction_mode', 'lifecycle_admission', 'proactive_cache_reclaim',
                  'resident_capacity_accounting', 'lifecycle_reservation_scale')
PLACEMENT_FIELDS = (
    'placement_cpu_overcommit', 'template_balance_slack', 'session_compute_nodes',
    'tool_call_cpu_policy', 'tool_call_cpu_pressure_ratio',
    'tool_call_cpu_pressure_samples', 'tool_call_cpu_group_max_active',
    'tool_call_cpu_min_duration_seconds', 'tool_call_cpu_include_smt',
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
                   policy=(Literal['os_default', 'round_robin', 'explicit', 'load_aware'], 'round_robin'),
                   template_affinity=(bool, False))
IdleReclaim = subset('IdleReclaim', ResourcesSpec, RESOURCE_FIELDS['idle_reclaim'],
                     policy=(Literal['off', 'wait'], 'off'),
                     scope=(Literal['tool', 'agent_and_tool'], 'tool'))
SharedMemory = subset('SharedMemory', ResourcesSpec, RESOURCE_FIELDS['shared_memory'],
                      borrow=(Literal['off', 'hard', 'pressure'], 'off'))
Checkpoint = subset('Checkpoint', ResourcesSpec, RESOURCE_FIELDS['checkpoint'],
                    mechanism=(Literal['full-copy', 'incremental-cow'], 'incremental-cow'),
                    storage=(Literal['disk', 'warm-only', 'tiered'], 'disk'),
                    residency=(Literal['lru', 'value'], 'lru'))


class Deployment(StrictFrozenModel):
    mode: Literal['managed', 'os-default'] = 'managed'
    resident_capacity_accounting: bool = False
    lifecycle_reservation_scale: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False)


class Coordination(StrictFrozenModel):
    action_selection: Literal['reclaim_first', 'measured_cost'] = 'reclaim_first'


class Conditions(StrictFrozenModel):
    prediction_source: Literal['kb', 'cold-start-kb', 'command-table'] = 'kb'


class Mechanisms(StrictFrozenModel):
    admission: Admission = Field(default_factory=Admission)
    runtime: Runtime = Field(default_factory=Runtime)
    placement: Placement = Field(default_factory=Placement)
    idle_reclaim: IdleReclaim = Field(default_factory=IdleReclaim)
    shared_memory: SharedMemory = Field(default_factory=SharedMemory)
    checkpoint: Checkpoint = Field(default_factory=Checkpoint)
    deployment: Deployment = Field(default_factory=Deployment)
    coordination: Coordination = Field(default_factory=Coordination)


owned = {key for fields in RESOURCE_FIELDS.values() for key in fields}
EnvironmentResources = subset('EnvironmentResources', ResourcesSpec,
                              set(ResourcesSpec.model_fields) - owned - set(EVIDENCE_FIELDS) - set(DERIVED_FIELDS))
DesignExecution = subset('DesignExecution', ExecutionSpec,
                          set(ExecutionSpec.model_fields) - set(PLACEMENT_FIELDS) - set(MEASUREMENT_FIELDS) - {'placement_policy'})
Evidence = subset('Evidence', ResourcesSpec, EVIDENCE_FIELDS)
Measurement = subset('Measurement', ExecutionSpec, MEASUREMENT_FIELDS,
                     observe_predictions=(bool, False),
                     validation=(ValidationSpec, Field(default_factory=ValidationSpec)),
                     output=(OutputSpec, Field(default_factory=OutputSpec)))


class Environment(StrictFrozenModel):
    runtime: RuntimeSandboxSpec
    sandbox: SandboxSpec
    resources: EnvironmentResources


class DesignArm(StrictFrozenModel):
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    override: dict[str, Any] = Field(default_factory=dict)


class Comparison(StrictFrozenModel):
    recipe: Literal['admission', 'runtime', 'memory-reclaim', 'placement', 'idle-reclaim', 'shared-memory', 'coordination', 'checkpoint', 'integrated', 'resident-capacity', 'custom'] = 'custom'
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
    if pl['tool_call_cpu_policy'] == 'c2':
        if not r['compute_nodes'] or not r['local_memory_cgroup']:
            raise ValueError(
                f'{name}: C2 Tool-call CPU placement requires compute_nodes and '
                'the verified local_memory_cgroup path'
            )
        if pl['policy'] == 'os_default':
            raise ValueError(f'{name}: C2 Tool-call CPU placement requires managed NUMA placement')
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
        'kb': 'runtime', 'cold-start-kb': 'cold-start', 'command-table': 'frozen',
    }[design.conditions.prediction_source]
    r['proactive_cache_reclaim'] = rt['proactive_memory_reclaim']
    if r['resident_capacity_accounting']:
        # The static ledger uses allocated LOCAL capacity, not pressure thresholds.
        r['pool_memory_budget_mib'] = r['local_memory_capacity_mib'] or r['pool_memory_budget_mib']
    if rt['cpu_release_boost'] and (pl['policy'] == 'os_default' or
            (not r['compute_nodes'] and r['local_numa_node'] is None)):
        raise ValueError(f'{name}: cpu_release_boost requires fixed LOCAL NUMA placement; configure compute_nodes or local_numa_node and use managed placement')
    if coordination['action_selection'] == 'measured_cost' and (
            shared['borrow'] == 'hard' or not (snapshots or shared['borrow'] == 'pressure')
            or not e['memory_action_costs']):
        raise ValueError(f'{name}: measured-cost selection needs reclaim or pressure borrowing and empirical memory_action_costs')
    if shared['borrow'] != 'off' and not r['shared_memory_borrow_limit_mib']:
        raise ValueError(f'{name}: borrowing requires a positive environment shared_memory_borrow_limit_mib')
    if snapshots and ck['residency'] == 'value' and ck['storage'] != 'tiered':
        raise ValueError(f'{name}: value residency requires idle reclaim and tiered storage')
    if snapshots and ck['storage'] != 'disk' and (not r['warm_memory_capacity_mib'] or not r['warm_snapshot_root']):
        raise ValueError(f'{name}: WARM storage requires configured pool capacity and path')
    if snapshots and ck['storage'] in {'disk', 'tiered'} and not r['cold_snapshot_root']:
        raise ValueError(f'{name}: disk/tiered snapshots require cold_snapshot_root')
    if shared['borrow'] == 'off':
        r['shared_memory_borrow_limit_mib'] = None
    r['memory_action_costs'] = e['memory_action_costs'] if coordination['action_selection'] == 'measured_cost' else None
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
        r.update(snapshot_mechanism='full-copy', lazy_restore=True,
                 snapshot_storage='tiered', snapshot_destination='cold')
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
             eviction='wait_aware_pressure' if snapshots else 'none', restore='reactive' if snapshots else 'none',
             template_aware_placement=pl['template_affinity'], snapshot_aware_residency=snapshots and ck['residency'] == 'value',
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
                output=d.measurement.output.model_dump(mode='json'))


def export_design(spec):
    """Freeze explicit effective arms, including qualification workload edits."""
    raw = deepcopy(spec.design)
    raw['experiment_id'] = spec.experiment_id
    raw['workload'] = spec.workload.model_dump(mode='json')
    raw['inference'] = spec.inference.model_dump(mode='json')
    raw['execution'] = {key: getattr(spec.execution, key) for key in DesignExecution.model_fields}
    raw['measurement'].update({key: getattr(spec.execution, key) for key in MEASUREMENT_FIELDS})
    raw['measurement']['validation'] = spec.validation.model_dump(mode='json')
    raw['measurement']['output'] = spec.output.model_dump(mode='json')
    common = raw['mechanisms']
    declared = {arm['name']: arm.get('override', {}) for arm in raw['comparison']['arms']}
    raw['comparison']['arms'] = [dict(name=p.name, override={
        f'{dimension}.{key}': value for dimension in GROUPS
        for key, value in p.mechanisms[dimension].items()
        if common[dimension][key] != value or f'{dimension}.{key}' in declared.get(p.name, {})}) for p in spec.policies]
    return Design.model_validate(raw).model_dump(mode='json')


def recipe_arms(recipe, *, checkpoint_axis=None):
    def arm(name, **fields):
        fields.setdefault('placement__placement_cpu_overcommit',
                          ExecutionSpec.model_fields['placement_cpu_overcommit'].default)
        return dict(name=name, override={key.replace('__', '.'): value for key, value in fields.items()})
    if recipe == 'resident-capacity':
        neutral = dict(admission__tool='off', admission__startup_headroom_mib=0,
                       runtime__memory_protection=False, runtime__cpu_release_boost=False,
                       runtime__proactive_memory_reclaim=False,
                       placement__policy='round_robin', placement__template_affinity=False,
                       placement__tool_call_cpu_policy='c0', placement__session_compute_nodes=[],
                       idle_reclaim__policy='off', shared_memory__borrow='off',
                       deployment__mode='managed', deployment__resident_capacity_accounting=True,
                       coordination__action_selection='reclaim_first')
        return [arm('resident-capacity-'+label.replace('.', '-'), **neutral, deployment__lifecycle_reservation_scale=scale)
                for label, scale in (('1', 1.0), ('0.8', .8), ('0.75', .75))]
    if recipe == 'admission':
        return [arm('admission-'+mode, admission__tool=mode) for mode in ('off', 'fixed', 'predicted')]
    if recipe == 'runtime':
        return [arm('memory-off-cpu-off', runtime__memory_protection=False, runtime__cpu_release_boost=False, runtime__proactive_memory_reclaim=False),
                arm('memory-protection', runtime__memory_protection=True, runtime__cpu_release_boost=False, runtime__proactive_memory_reclaim=False),
                arm('release-cpu', runtime__memory_protection=False, runtime__cpu_release_boost=True, runtime__proactive_memory_reclaim=False),
                arm('memory-and-cpu', runtime__memory_protection=True, runtime__cpu_release_boost=True, runtime__proactive_memory_reclaim=False)]
    if recipe == 'memory-reclaim':
        return [arm('memory-reclaim-'+mode, runtime__proactive_memory_reclaim=enabled)
                for mode, enabled in (('off', False), ('on', True))]
    if recipe == 'placement':
        return [arm('round-robin', placement__policy='round_robin', placement__template_affinity=False),
                arm('load-aware', placement__policy='load_aware', placement__template_affinity=False),
                arm('load-aware-template-affinity', placement__policy='load_aware', placement__template_affinity=True)]
    if recipe == 'idle-reclaim':
        return [arm('resident', idle_reclaim__policy='off'),
                arm('wait-tool', idle_reclaim__policy='wait', idle_reclaim__scope='tool'),
                arm('wait-pair', idle_reclaim__policy='wait', idle_reclaim__scope='agent_and_tool')]
    if recipe == 'shared-memory':
        return [arm('local', shared_memory__borrow='off', coordination__action_selection='reclaim_first'),
                arm('hard-borrow', shared_memory__borrow='hard', coordination__action_selection='reclaim_first'),
                arm('pressure-borrow', shared_memory__borrow='pressure', coordination__action_selection='reclaim_first')]
    if recipe == 'coordination':
        return [arm('action-'+mode, coordination__action_selection=mode)
                for mode in ('reclaim_first', 'measured_cost')]
    if recipe == 'checkpoint':
        if checkpoint_axis not in {'encoding', 'storage', 'residency'}:
            raise ValueError('checkpoint recipe requires --checkpoint-axis encoding|storage|residency')
        field, values = {'encoding': ('mechanism', ('full-copy', 'incremental-cow')),
                         'storage': ('storage', ('disk', 'warm-only', 'tiered')),
                         'residency': ('residency', ('lru', 'value'))}[checkpoint_axis]
        return [arm('checkpoint-'+value, **{'checkpoint__'+field: value}) for value in values]
    if recipe == 'integrated':
        neutral = {'deployment__resident_capacity_accounting': False,
                   'deployment__lifecycle_reservation_scale': 1.0, 'admission__tool': 'off',
                   'runtime__memory_protection': False, 'runtime__cpu_release_boost': False,
                   'runtime__proactive_memory_reclaim': False, 'placement__template_affinity': False,
                   'placement__tool_call_cpu_policy': 'c0', 'idle_reclaim__policy': 'off',
                   'shared_memory__borrow': 'off', 'coordination__action_selection': 'reclaim_first',
                   'checkpoint__residency': 'lru'}
        return [arm('os-default', **neutral, placement__policy='os_default', deployment__mode='os-default', placement__session_compute_nodes=[]),
                arm('managed-neutral', **neutral, deployment__mode='managed', placement__policy='round_robin', placement__session_compute_nodes=[]),
                arm('configured-design')]
    return [arm('configured-design')]


def fixed_recipe_arms(recipe, *, common, checkpoint_axis=None):
    """Materialize controlled studies; retain common tuning, reset policy switches."""
    if recipe in {'custom', 'integrated'}:
        raise ValueError('--fixed-methods requires a single-family recipe; custom/integrated inherit user choices')
    switches = {
        'deployment.mode': 'managed', 'deployment.resident_capacity_accounting': False,
        'deployment.lifecycle_reservation_scale': 1.0, 'coordination.action_selection': 'reclaim_first',
        'admission.tool': 'off', 'runtime.memory_protection': False,
        'runtime.cpu_release_boost': False, 'runtime.proactive_memory_reclaim': False,
        'placement.policy': 'round_robin', 'placement.template_affinity': False,
        'placement.tool_call_cpu_policy': 'c0',
        'placement.session_compute_nodes': [], 'idle_reclaim.policy': 'off',
        'idle_reclaim.scope': 'tool', 'shared_memory.borrow': 'off',
        'checkpoint.mechanism': 'full-copy', 'checkpoint.storage': 'disk',
        'checkpoint.residency': 'lru',
    }
    if recipe in {'runtime', 'memory-reclaim'}:
        switches['admission.tool'] = 'fixed'
    if recipe in {'checkpoint', 'coordination'}:
        switches['idle_reclaim.policy'] = 'wait'
    if recipe == 'coordination':
        switches.update({'shared_memory.borrow': 'pressure', 'checkpoint.storage': 'tiered'})
    if recipe == 'checkpoint' and checkpoint_axis == 'residency':
        switches['checkpoint.storage'] = 'tiered'
    neutral = override_mechanisms(common, switches)
    result = []
    for arm in recipe_arms(recipe, checkpoint_axis=checkpoint_axis):
        resolved = override_mechanisms(neutral, arm['override'])
        result.append(dict(name=arm['name'], override={
            f'{group}.{field}': value for group in GROUPS
            for field, value in resolved[group].items()}))
    return result


def design_overview(spec):
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
        costs = resolved.resources.snapshot_residency_costs
        if p.snapshot_aware_residency and costs and costs.cold_restore_seconds_per_gib <= costs.warm_restore_seconds_per_gib:
            warnings.append('Measured COLD restore is no slower than WARM: retention value is zero.')
        rows.append(dict(name=p.name, mechanisms=p.mechanisms, requirements=requirements, warnings=warnings,
                         differences={f'{d}.{k}': v for d in GROUPS for k, v in p.mechanisms[d].items()
                                      if common[d][k] != v},
                         effective_resources=resolved.resources.model_dump(mode='json'),
                         effective_placement=resolved.execution.placement_policy))
    return dict(experiment_id=spec.experiment_id, recipe=spec.design['comparison']['recipe'],
                dimensions=list(DIMENSIONS), control_groups=['deployment', 'coordination'],
                conditions=spec.design['conditions'], arms=rows, arm_count=len(expand_matrix(spec)),
                environment=spec.design['environment'], evidence=spec.design['evidence'],
                execution=spec.design['execution'])


def configure_design(raw, options):
    """Use the existing configure command; never launch an alternate runner."""
    from .spec import ExperimentSpec
    from .configure import parse_concurrency, gib_to_mib, calibrated_static_tool_memory
    recipe = options.get('recipe')
    if options.get('suite') or options.get('baseline_names') or options.get('runtime_control'):
        raise ValueError('six-dimension designs use --recipe and --set, not --suite/--baseline/--runtime-control')
    if options.get('suite_ablations') is not None:
        raise ValueError('select explicit comparison.arms instead of --ablations')
    if raw.get('schema_version') != 4:
        if not recipe:
            raise ValueError('select --recipe to create a six-dimension design')
        r = raw['resources']
        x = raw.get('execution', {})
        raw = dict(schema_version=4, experiment_id=raw.get('experiment_id', 'experiment'),
                   environment=dict(runtime=raw['runtime'], sandbox=raw['sandbox'],
                                    resources={k: v for k, v in r.items() if k in EnvironmentResources.model_fields}),
                   workload=raw['workload'], agent=raw.get('agent', {}), inference=raw['inference'],
                   evidence={k: v for k, v in r.items() if k in Evidence.model_fields},
                   measurement=dict(validation=raw.get('validation', {}), output=raw.get('output', {}),
                                    **{k: v for k, v in x.items() if k in MEASUREMENT_FIELDS}),
                   mechanisms=Mechanisms().model_dump(mode='json'),
                   execution={k: v for k, v in x.items() if k in DesignExecution.model_fields},
                   comparison=dict(recipe=recipe, arms=[]))
        # Import explicit scalar parameters, not legacy preset/suite semantics.
        for dimension, fields in RESOURCE_FIELDS.items():
            raw['mechanisms'][dimension].update({k: r[k] for k in fields if k in r})
    raw = deepcopy(raw)
    env = raw['environment']
    resources = env['resources']
    profile = options.get('host_profile')
    if profile:
        for key in ('local_memory_capacity_mib', 'local_memory_low_watermark_mib',
                    'local_memory_high_watermark_mib', 'shared_memory_borrow_limit_mib',
                    'local_numa_node', 'warm_numa_node', 'local_memory_cgroup'):
            resources[key] = profile.get(key)
        resources.update(compute_nodes=profile.get('compute_nodes') or [],
                         warm_memory_capacity_mib=profile.get('warm_capacity_mib', 0),
                         warm_snapshot_root=profile.get('warm_root'), cold_snapshot_root=profile.get('cold_root'),
                         pool_memory_budget_mib=profile.get('local_memory_high_watermark_mib') or profile['local_memory_capacity_mib'])
    m = Mechanisms.model_validate(raw.get('mechanisms', {})).model_dump(mode='json')
    evidence = raw.setdefault('evidence', {})
    execution = raw.setdefault('execution', {})
    measurement = raw.setdefault('measurement', {})
    conditions = raw.setdefault('conditions', {})
    if options.get('prediction_mode') is not None:
        conditions['prediction_source'] = {
            'runtime': 'kb', 'cold-start': 'cold-start-kb', 'frozen': 'command-table',
        }[options['prediction_mode']]
    if options.get('template_base_sharing') is not None:
        raise ValueError('schema 4 derives template sharing qualification from placement.template_affinity; remove --template-base-sharing')
    # Existing ergonomic scalar options retain their units; all mechanism
    # parameters are also accessible through --set or editable YAML.
    for key in EnvironmentResources.model_fields:
        if options.get(key) is not None:
            resources[key] = options[key]
    for dimension, fields in RESOURCE_FIELDS.items():
        for key in fields:
            if options.get(key) is not None:
                m[dimension][key] = options[key]
    for key in Evidence.model_fields:
        if options.get(key) is not None:
            if options[key] == 'auto':
                raise ValueError(f'{key}=auto is not allowed: supply independent evidence')
            evidence[key] = options[key]
    for option, field in (('warm_restore_seconds_per_gib', 'warm_restore_seconds_per_gib'),
                          ('cold_restore_seconds_per_gib', 'cold_restore_seconds_per_gib'),
                          ('snapshot_cost_provenance', 'provenance')):
        if options.get(option) is not None:
            evidence.setdefault('snapshot_residency_costs', {})
            if evidence['snapshot_residency_costs'] is None:
                evidence['snapshot_residency_costs'] = {}
            evidence['snapshot_residency_costs'][field] = options[option]
    for key in DesignExecution.model_fields:
        if options.get(key) is not None:
            execution[key] = options[key]
    for key in MEASUREMENT_FIELDS:
        if options.get(key) is not None:
            measurement[key] = options[key]
    for option, field in (('pool_memory_gib', 'pool_memory_budget_mib'),
                          ('emergency_free_memory_gib', 'emergency_free_memory_mib')):
        if options.get(option) is not None:
            resources[field] = gib_to_mib(options[option], name=option)
    if options.get('checkpoint_headroom_gib') is not None:
        m['idle_reclaim']['checkpoint_restore_headroom_mib'] = gib_to_mib(options['checkpoint_headroom_gib'], name='checkpoint headroom', allow_zero=True)
    for option, field in (('placement_policy', 'policy'), ('session_compute_nodes', 'session_compute_nodes'),
                          ('template_balance_slack', 'template_balance_slack'),
                          ('template_aware_placement', 'template_affinity')):
        if options.get(option) is not None:
            m['placement'][field] = options[option]
    if options.get('memory_scheduling') is not None:
        raise ValueError('use --set shared_memory.borrow=... and --set idle_reclaim.policy=... instead of --memory-scheduling')
    for option, field in (('snapshot_mechanism', 'mechanism'), ('snapshot_storage', 'storage')):
        if options.get(option) is not None:
            m['checkpoint'][field] = options[option]
    if options.get('snapshot_aware_residency') is not None:
        m['checkpoint']['residency'] = 'value' if options['snapshot_aware_residency'] else 'lru'
    for role, prefix in (('runtime', 'runtime'), ('sandbox', 'tool')):
        vm = env[role]
        for suffix, field in (('template_id', 'template_id'), ('image_reference', 'source_image_reference'),
                              ('image_digest', 'image_digest'), ('vcpu', 'vcpu')):
            if options.get(prefix+'_'+suffix) is not None:
                vm[field] = options[prefix+'_'+suffix]
                if field == 'template_id':
                    vm.pop('template_alias', None)
        if options.get(prefix+'_memory_gib') is not None:
            vm['memory_mib'] = gib_to_mib(options[prefix+'_memory_gib'], name=prefix+' memory')
    if options.get('tool_workspace') is not None:
        env['sandbox']['workspace'] = options['tool_workspace']
    workload = raw['workload']
    if options.get('repetitions') is not None:
        workload['repetitions'] = options['repetitions']
    if options.get('session_assignment') is not None:
        workload['session_assignment'] = options['session_assignment']
    if options.get('concurrency') is not None:
        execution['concurrency_levels'] = parse_concurrency(options['concurrency'])
    if options.get('stagger_seconds') is not None:
        execution['stagger_interval_seconds'] = options['stagger_seconds']
    if options.get('experiment_id') is not None:
        raw['experiment_id'] = options['experiment_id']
    single_case_keys = ('trace', 'case_id', 'prompt', 'repository', 'base_commit')
    if options.get('workload_manifest') is not None and any(
        options.get(key) is not None for key in single_case_keys
    ):
        raise ValueError('--workload-manifest cannot be combined with single-case workload overrides')
    if options.get('workload_manifest') is not None:
        workload.update(
            source='swe_rebench', input=options['workload_manifest'],
            session_assignment=options.get('session_assignment') or 'unique',
        )
        workload.pop('cases', None)
    elif any(options.get(k) is not None for k in single_case_keys):
        cases = workload.get('cases')
        if not cases and workload['source'] == 'recorded_trace':
            trace = options.get('trace') or workload['input']
            cases = [dict(case_id=Path(trace).stem, source='recorded_trace', source_reference=trace, replay_trace_reference=trace)]
            workload['cases'] = cases
        if not cases or len(cases) != 1:
            raise ValueError('workload overrides require one case; edit workload.cases for heterogeneous tasks')
        for key in ('case_id', 'prompt', 'repository', 'base_commit'):
            if options.get(key) is not None:
                cases[0][key] = options[key]
        if options.get('trace'):
            workload.update(source='recorded_trace', input=options['trace'])
            cases[0].update(source='recorded_trace', source_reference=options['trace'], replay_trace_reference=options['trace'])
    if options.get('validation_command') is not None:
        measurement['validation'] = dict(command=options['validation_command'])
    inference = raw['inference']
    if options.get('inference_backend') is not None:
        inference['backend'] = options['inference_backend']
    for key in ('model', 'base_url', 'api_key_env', 'time_scale', 'openclaw_exec_yield_ms'):
        if options.get(key) is not None:
            inference.setdefault('configuration', {})[key] = options[key]
    changes = {}
    for setting in options.get('mechanism_settings') or ():
        key, sep, value = setting.partition('=')
        if not sep:
            raise ValueError('--set requires dimension.field=YAML_VALUE')
        parsed = value if value in {'off', 'on'} else yaml.safe_load(value)
        if key.startswith('conditions.'):
            conditions[key.removeprefix('conditions.')] = parsed
        else:
            changes[key] = parsed
    # Resolve measured fixed allowance before strict mechanism validation.
    if 'admission.static_tool_memory_mib' in changes:
        m['admission']['static_tool_memory_mib'] = changes['admission.static_tool_memory_mib']
    if (recipe == 'admission' or (options.get('fixed_methods') and recipe in {'runtime', 'memory-reclaim'})) and m['admission']['static_tool_memory_mib'] is None:
        m['admission']['static_tool_memory_mib'] = 'auto'
    if m['admission']['static_tool_memory_mib'] == 'auto':
        if not evidence.get('prediction_artifact'):
            raise ValueError('fixed auto allowance requires evidence.prediction_artifact')
        cases = workload.get('cases') or []
        repo = cases[0].get('repository') if len(cases) == 1 else None
        if not repo:
            raise ValueError('fixed auto allowance requires one repository')
        m['admission']['static_tool_memory_mib'] = calibrated_static_tool_memory(Path(evidence['prediction_artifact']), repo)
    raw['mechanisms'] = override_mechanisms(m, changes)
    if options.get('fixed_methods') and not recipe:
        raise ValueError('--fixed-methods requires --recipe')
    if recipe:
        if recipe == 'checkpoint' and not options.get('fixed_methods') and raw['mechanisms']['idle_reclaim']['policy'] == 'off':
            raise ValueError('checkpoint comparison requires idle reclaim; set idle_reclaim.policy=wait before selecting this recipe')
        if options.get('fixed_methods'):
            if conditions.get('prediction_source', 'kb') == 'command-table':
                raise ValueError('fixed methods require a KB prediction source; command-table is diagnostic')
            arms = fixed_recipe_arms(recipe, common=raw['mechanisms'], checkpoint_axis=options.get('checkpoint_axis'))
        else:
            arms = recipe_arms(recipe, checkpoint_axis=options.get('checkpoint_axis'))
        raw['comparison'] = dict(recipe=recipe, arms=arms)
    if options.get('checkpoint_axis') and recipe != 'checkpoint':
        raise ValueError('--checkpoint-axis requires --recipe checkpoint')
    spec = ExperimentSpec.model_validate(raw)
    for path in changes:
        fields = path.split('.')
        requested = raw['mechanisms']
        for field in fields:
            requested = requested[field]
        overridden = []
        for policy in spec.policies:
            effective = policy.mechanisms
            for field in fields:
                effective = effective[field]
            if effective != requested:
                overridden.append(f'{policy.name}={effective!r}')
        if overridden:
            warnings.warn(
                f'--set {path}={requested!r} is overridden by comparison arms: '
                + ', '.join(overridden) + '; edit comparison.arms[].override to change these arms.',
                UserWarning, stacklevel=2,
            )
    return spec
