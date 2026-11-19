"""One explicit baseline and one workload, compiled into the existing worker."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from .design import (
    Conditions, DesignExecution, Environment, Evidence, Measurement,
    Mechanisms, subset,
)
from .spec import (
    AgentSpec, ExperimentSpec, InferenceSpec, StrictFrozenModel, ValidationSpec,
    WorkloadSpec,
)


SUBMISSION_FIELDS = {
    'concurrency_levels', 'max_active_sessions', 'arrival_schedule',
    'normal_arrival_std_seconds', 'session_arrival_offsets_seconds',
    'stagger_interval_seconds', 'poisson_arrival_window_fraction',
    'poisson_arrival_window_seconds', 'random_seed', 'randomized_order',
    'measurement_window_seconds',
}
Submission = subset('Submission', DesignExecution, SUBMISSION_FIELDS)
BaselineExecution = subset('BaselineExecution', DesignExecution,
                           set(DesignExecution.model_fields) - SUBMISSION_FIELDS)
BaselineMeasurement = subset('BaselineMeasurement', Measurement,
                             set(Measurement.model_fields) - {'validation'})


class BaselineConfig(StrictFrozenModel):
    kind: Literal['clawbox-baseline'] = 'clawbox-baseline'
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    environment: Environment
    agent: AgentSpec = Field(default_factory=AgentSpec)
    inference: InferenceSpec
    evidence: Evidence = Field(default_factory=Evidence)
    measurement: BaselineMeasurement = Field(default_factory=BaselineMeasurement)
    mechanisms: Mechanisms = Field(default_factory=Mechanisms)
    conditions: Conditions = Field(default_factory=Conditions)
    execution: BaselineExecution = Field(default_factory=BaselineExecution)


class WorkloadConfig(StrictFrozenModel):
    kind: Literal['clawbox-workload'] = 'clawbox-workload'
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    tasks: WorkloadSpec
    submission: Submission = Field(default_factory=Submission)
    validation: ValidationSpec = Field(default_factory=ValidationSpec)


def read_document(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f'cannot read configuration {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{path}: configuration must be a YAML mapping')
    return value


def _file(value: str | None, root: Path) -> str | None:
    if not value:
        return value
    path = Path(value).expanduser()
    # Absolute Linux paths describe the target host, even when validated on Windows.
    if value.startswith('/'):
        return value
    if path.is_absolute():
        return str(path)
    return str((root / path).resolve())


def load_configuration(baseline_path: Path, workload_path: Path) -> ExperimentSpec:
    """Resolve input files relative to their owning document, never the shell cwd."""
    baseline = BaselineConfig.model_validate(read_document(baseline_path)).model_dump(mode='json')
    workload = WorkloadConfig.model_validate(read_document(workload_path)).model_dump(mode='json')
    baseline_root, workload_root = baseline_path.resolve().parent, workload_path.resolve().parent
    evidence = baseline.get('evidence', {})
    for key in ('prediction_artifact', 'template_base_sharing_artifact'):
        if evidence.get(key):
            evidence[key] = _file(evidence[key], baseline_root)
    inference = baseline.get('inference', {}).get('configuration', {})
    for key in ('clawtune_config', 'serving_sim_catalog_manifest'):
        if inference.get(key):
            inference[key] = _file(inference[key], baseline_root)
    placement = baseline['execution'].get('tool_placement')
    if placement:
        for key in ('targets_file', 'prefix_sequences_file'):
            placement[key] = _file(placement[key], baseline_root)
    tasks = workload.get('tasks', {})
    if tasks.get('input'):
        tasks['input'] = _file(tasks['input'], workload_root)
    for case in tasks.get('cases', []):
        for key in ('replay_trace_reference',):
            if case.get(key):
                case[key] = _file(case[key], workload_root)
        # Branching reads source_reference as a scenario file even for synthetic
        # cases. Other non-replay sources may use opaque provenance identifiers.
        if (case.get('source') == 'recorded_trace'
                or baseline['execution'].get('snapshot_branching') is not None):
            case['source_reference'] = _file(case['source_reference'], workload_root)
    return compile_configuration(BaselineConfig.model_validate(baseline),
                                 WorkloadConfig.model_validate(workload))


def compile_configuration(baseline: BaselineConfig, workload: WorkloadConfig) -> ExperimentSpec:
    raw = baseline.model_dump(mode='json')
    raw.pop('kind')
    name = raw.pop('name')
    raw.update(schema_version=4, experiment_id=f'{name}-{workload.name}',
               workload=workload.tasks.model_dump(mode='json'),
               comparison={'recipe': 'custom', 'arms': [{'name': name}]})
    raw['execution'].update(workload.submission.model_dump(mode='json'))
    raw['measurement']['validation'] = workload.validation.model_dump(mode='json')
    return ExperimentSpec.model_validate(raw)


def freeze_experiment(spec: ExperimentSpec) -> dict:
    """Internal, self-contained run snapshot. Not another editable input interface."""
    from .design import export_design
    # Qualification may change workload/limits on a model_copy; recompile the
    # exported design so serialized resources and arm identities stay consistent.
    if spec.design is not None:
        spec = ExperimentSpec.model_validate(export_design(spec))
    return {'kind': 'clawbox-resolved-experiment', 'schema_version': 5,
            'specification': spec.model_dump(mode='json')}


def load_frozen(path: Path) -> ExperimentSpec:
    raw = read_document(path)
    if set(raw) != {'kind', 'schema_version', 'specification'} or (
        raw.get('kind') != 'clawbox-resolved-experiment' or raw.get('schema_version') != 5
    ):
        raise ValueError('use --baseline BASELINE.yaml --workload WORKLOAD.yaml; '
                         'positional inputs must be a resolved run snapshot (schema 5)')
    return ExperimentSpec.model_validate(raw['specification'])


def dump_experiment(spec: ExperimentSpec, *, compact: bool = False) -> str:
    return yaml.safe_dump(freeze_experiment(spec), sort_keys=False, allow_unicode=True)


def experiment_overview(spec: ExperimentSpec) -> dict:
    from .design import design_overview
    return design_overview(spec)
