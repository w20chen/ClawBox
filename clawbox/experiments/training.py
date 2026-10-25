"""Train frozen P50 admission inputs from ClawBox CubeSandbox measurements."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from clawbox.replay.trace import load_trace
from clawbox.tuning.__main__ import find_run_datasets
from clawbox.tuning.dataset import build_joined_dataset
from clawbox.tuning.native import native_clause_observations
from clawbox.tuning.clawtune import observation_to_completed_call, predict_native_call_load_models
from clawbox.clawtune_integration import source_revision, use_clawtune
from .prediction import command_sha256, select_p50, PredictionUnavailable
from .results import ResultEnvelope, RunStatus
from .spec import ExperimentSpec, load_experiment, spec_digest


@dataclass(frozen=True)
class TrainingArmSource:
    run_root: Path
    artifact_root: Path
    result: ResultEnvelope


COLLECTOR_SEMANTICS = {
    'resource_schema': 'cgroup_resource_v1',
    'resource_source': 'cgroup-v2',
    'cpu_total_counter': (
        'cpu.stat usage_usec authoritative total retained separately from '
        'user_usec and system_usec'
    ),
    'memory_measurement': 'guest_memtotal_minus_memavailable',
    'memory_scope': 'isolated writable CubeSandbox Tool VM environment',
    'clause_telemetry_schema': 'clause_telemetry_v2',
    'join': 'exact execution_id',
    'required_pmu_events': [
        'cycles', 'instructions', 'llc_read_accesses', 'llc_read_misses',
    ],
    'training_label_policy': 'clawtune_available_or_zero',
    'quality_markers': 'retained as diagnostics; partial observations train',
}


def _clawtune_revision() -> str:
    root = Path(os.environ.get(
        'CLAWTUNE_ROOT', str(Path(__file__).resolve().parents[2] / 'ClawTune'),
    ))
    revision = source_revision(root)
    configured = os.environ.get('CLAWTUNE_REVISION')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('training requires an exact ClawTune source revision')
    if configured and configured != revision:
        raise ValueError(
            f'configured ClawTune revision {configured} differs from source {revision}'
        )
    return revision


def _percentile(values: list[float], quantile: float) -> float:
    """Return the linear empirical quantile used for frozen calibration."""
    if not values:
        raise ValueError('cannot calculate a percentile without observations')
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _read_json_object(path: Path, *, description: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'cannot read {description} {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{path}: {description} must be an object')
    return value


def _exact_command_memory_model(samples: list[dict]) -> dict | None:
    """Build a frozen exact-command estimate from trusted Cube measurements."""
    from clawtune_sidecar.monitoring.environment_memory import memory_labels

    values = []
    for sample in samples:
        value = memory_labels(sample).get('memory_extra_peak_bytes')
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0):
            values.append(float(value))
    if len(values) < 2:
        return None
    return {
        'schema_version': 'call_load.v2',
        'memory_measurement': 'guest_memtotal_minus_memavailable',
        'scope': 'tool_call',
        'lifecycle': 'tool_hook_interval',
        'targets': {
            'memory_extra_peak_bytes': {
                'status': 'available',
                'unit': 'bytes',
                'metric_definition': 'environment_memory_peak_minus_baseline',
                'avg': sum(values) / len(values),
                'p50': _percentile(values, .5),
                'p90': _percentile(values, .9),
                'backend': 'runtime',
                'method': 'validated_exact_command_empirical',
                'sample_count': len(values),
                'context': ['public', 'exact_command'],
                'assumptions': [],
                'calibration': 'validated_training_run',
                'unavailable_reason': None,
            },
        },
    }


def _training_arm_sources(run: Path, repository: str) -> tuple[ExperimentSpec, tuple[TrainingArmSource, ...]]:
    """Resolve only current supervised CubeSandbox runs into per-arm datasets."""
    run = run.resolve()
    spec_path = run / 'experiment.yaml'
    summary_path = run / 'summary.json'
    state_path = run / 'run-state.json'
    if not spec_path.is_file() or not summary_path.is_file() or not state_path.is_file():
        raise ValueError(f'{run}: expected a current supervised ClawBox CubeSandbox run')

    spec = load_experiment(spec_path)
    if (
        spec.experiment_id.endswith('-qualification')
        and spec.inference.configuration.get('max_model_steps') == 1
        and spec.validation.command == 'true'
    ):
        raise ValueError(f'{run}: qualification prefix runs cannot be used for training')
    if not spec.validation.command:
        raise ValueError(f'{run}: training requires an explicit final validation.command')
    repos = {case.repository for case in spec.workload.cases}
    if repos != {repository}:
        raise ValueError(f'{run}: training run must contain only repository {repository}')

    state = _read_json_object(state_path, description='run state')
    if state.get('state') != 'succeeded':
        raise ValueError(f'{run}: training requires a terminal succeeded supervisor run')
    summary = _read_json_object(summary_path, description='run summary')
    arms = summary.get('arms')
    if not isinstance(arms, list) or not arms:
        raise ValueError(f'{run}: training run summary has no completed arms')

    expected_digest = spec_digest(spec)
    sources: list[TrainingArmSource] = []
    for raw in arms:
        try:
            result = ResultEnvelope.model_validate(raw)
        except ValueError as exc:
            raise ValueError(f'{run}: invalid supervised arm result: {exc}') from exc
        arm_id = result.arm.arm_id
        attempt_id = result.attempt_id
        if Path(arm_id).name != arm_id or Path(attempt_id).name != attempt_id:
            raise ValueError(f'{run}: invalid attempt or arm identity in run summary')
        if result.experiment_id != spec.experiment_id or result.arm.spec_digest != expected_digest:
            raise ValueError(f'{run}: arm {arm_id} does not match the frozen experiment')
        if result.status is not RunStatus.SUCCEEDED:
            raise ValueError(f'{run}: training arm {arm_id} did not succeed')
        if result.correctness.get('validation_passed') is not True:
            raise ValueError(f'{run}: training arm {arm_id} did not pass final validation')
        if result.correctness.get('cleanup_verified') is not True:
            raise ValueError(f'{run}: training arm {arm_id} cleanup was not verified')
        if result.arm.policy.admission.value not in {'off', 'fixed'}:
            raise ValueError(f'{run}: training requires non-predicted admission arms (off or fixed)')

        marker = run / 'arms' / f'{arm_id}.complete'
        try:
            marker_digest = marker.read_text(encoding='ascii').strip()
        except OSError as exc:
            raise ValueError(f'{run}: training arm {arm_id} has no verified completion marker') from exc
        if marker_digest != result.arm.spec_digest:
            raise ValueError(f'{run}: training arm {arm_id} completion marker does not match')

        artifact_root = run / 'attempts' / attempt_id / arm_id
        if not (artifact_root / 'owned-sandboxes.jsonl').is_file():
            raise ValueError(f'{run}: training arm {arm_id} has no ownership journal')
        try:
            datasets = find_run_datasets(artifact_root)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(f'{run}: training arm {arm_id} has no complete Cube dataset: {exc}') from exc
        if not datasets:
            raise ValueError(f'{run}: training arm {arm_id} has no complete Cube dataset')
        sources.append(TrainingArmSource(run, artifact_root, result))
    return spec, tuple(sources)


def trace_commands(path: Path) -> list[str]:
    commands = []
    for action in load_trace(path):
        if action.kind != 'llm':
            continue
        message = action.output
        if isinstance(message, dict) and isinstance(message.get('content'), dict):
            message = message['content']
        for call in (message or {}).get('tool_calls', []):
            function = call.get('function') or {}
            args = function.get('arguments') or {}
            if isinstance(args, str):
                args = json.loads(args)
            command = args.get('command')
            if isinstance(command, str) and command:
                commands.append(command)
    return list(dict.fromkeys(commands))


def train_p50(runs: list[Path], trace: Path, repository: str, output: Path) -> dict:
    use_clawtune()
    from tool_time.lattice_kb import LatticeTimeKB
    from tool_resource.runtime_kb import RuntimeToolResourceKB, ToolCallQuery
    from tool_resource.sdk import _validate_artifact as validate_artifact
    from clawtune_sidecar.monitoring.environment_memory import memory_labels

    calls, clauses, evidence = [], [], []
    short = defaultdict(list)
    seen = set()
    digest = hashlib.sha256()
    sandbox_identity = None
    validations = []
    for run in dict.fromkeys(p.resolve() for p in runs):
        spec, arm_sources = _training_arm_sources(run, repository)
        identity = {k: getattr(spec.sandbox, k) for k in (
            'template_id', 'image_digest', 'vcpu', 'memory_mib', 'architecture',
        )}
        if not identity['image_digest']:
            raise ValueError(f'{run}: missing immutable Tool image digest')
        if sandbox_identity is not None and identity != sandbox_identity:
            raise ValueError('Training runs use different Tool environments')
        sandbox_identity = identity
        for source in arm_sources:
            host_rows = {r['execution_id']: r for r in
                         source.result.performance.get('tool_execution_observations', [])}
            digest.update(json.dumps(host_rows, sort_keys=True, separators=(',', ':')).encode())
            validations.append({
                'run': str(run), 'attempt_id': source.result.attempt_id,
                'arm_id': source.result.arm.arm_id,
                'command': spec.validation.command, 'status': 'succeeded',
                'cleanup_verified': True,
            })
            for traces, bridge, resources in find_run_datasets(source.artifact_root):
                _, trusted = build_joined_dataset(traces, bridge, resource_dir=resources)
                for item in trusted:
                    if item.execution_id in seen:
                        continue
                    seen.add(item.execution_id)
                    if item.cgroup is None or not item.command:
                        continue
                    c = item.cgroup.model_dump(mode='json')
                    if c.get('memory_measurement') != 'guest_memtotal_minus_memavailable':
                        continue
                    cgpath = resources / f'cgroup-resource-{item.execution_id}.json'
                    artpath = resources / f'clause-telemetry-{item.execution_id}.json'
                    if not cgpath.exists():
                        continue
                    raw = json.loads(cgpath.read_text(encoding='utf-8'))
                    digest.update(cgpath.read_bytes())
                    native = json.loads(artpath.read_text(encoding='utf-8')) if artpath.is_file() else {}
                    matched = [row for row in native.get('calls', [])
                               if row.get('tool_call_id') == item.execution_id]
                    if len(matched) != 1:
                        raise ValueError(f'{artpath}: expected one qualified execution for {item.execution_id}')
                    call = observation_to_completed_call(item, repository, telemetry=matched[0])
                    calls.append(call)
                    short[item.command].append(raw)
                    memory = memory_labels(raw)
                    evidence.append({'execution_id': item.execution_id, 'command': item.command,
                                     'duration_ms': raw.get('duration_ms'),
                                     'memory_extra_peak_bytes': memory.get('memory_extra_peak_bytes'),
                                     'host_extra_peak_bytes': host_rows.get(item.execution_id, {}).get('host_cgroup_execution_increment_bytes'),
                                     'memory_unavailable_reason': raw.get('memory_unavailable_reason'),
                                     'observed_repository': item.repo_fingerprint, 'source': str(cgpath)})
                    if artpath.exists():
                        artifact = json.loads(artpath.read_text(encoding='utf-8'))
                        validate_artifact(artpath, artifact, expected_repo=repository)
                        digest.update(artpath.read_bytes())
                        for native in artifact.get('calls', []):
                            if native.get('tool_call_id') == item.execution_id:
                                clauses.extend(native_clause_observations(repository, native, raw))
    if not calls:
        raise ValueError('No trusted completed Cube tool calls')
    if not any(r['memory_extra_peak_bytes'] is not None for r in evidence):
        raise ValueError('No valid Cube guest extra-memory labels; collect before training')
    lattice = LatticeTimeKB.fit(clauses)
    runtime = RuntimeToolResourceKB.fit_public(calls)
    query_ts = max(c.ts_end for c in calls) + 1
    records, unavailable = [], []
    counts = Counter()
    for command in trace_commands(trace):
        query = ToolCallQuery(repo=repository, tool_name='exec', command=command,
                             ts_start=query_ts, memory_measurement='guest_memtotal_minus_memavailable')
        results = predict_native_call_load_models(runtime, query, lattice=lattice)
        models = {k: results[k].model_dump(mode='json') for k in ('lattice', 'tool')}
        exact_command = _exact_command_memory_model(short[command])
        if exact_command is not None:
            models['exact_command'] = exact_command
        # Compound commands intentionally have no single binary identity in
        # ClawTune. Query the same native ToolKB at outer-tool granularity as
        # the final measured fallback instead of inventing a fixed estimate.
        tool_class_query = ToolCallQuery(
            repo=repository, tool_name='exec', command=None,
            ts_start=query_ts,
            memory_measurement='guest_memtotal_minus_memavailable',
        )
        models['tool_class'] = predict_native_call_load_models(
            runtime, tool_class_query, lattice=lattice,
        )['tool'].model_dump(mode='json')
        samples = short[command]
        short_evidence = None
        if samples and all(r.get('memory_unavailable_reason') == 'no_in_execution_memory_sample'
                           and isinstance(r.get('duration_ms'), (int, float))
                           and 0 <= r['duration_ms'] <= 20 for r in samples):
            short_evidence = {'count': len(samples), 'max_duration_ms': max(r['duration_ms'] for r in samples),
                              'reason': 'no_in_execution_memory_sample'}
        record = {'command': command, 'command_sha256': command_sha256(command),
                  'models': models, 'short_call_evidence': short_evidence}
        try:
            selected = select_p50(models, short_call_evidence=short_evidence)
            record['selection'] = selected
            counts[selected['fallback_level']] += 1
        except PredictionUnavailable as exc:
            unavailable.append({'command': command, 'reason': str(exc)})
        records.append(record)
    snapshots = {'lattice': lattice.to_json_obj(), 'tool': runtime.to_json_obj()}
    kb_bytes = json.dumps(snapshots, sort_keys=True).encode()
    valid_memory = [
        float(row['memory_extra_peak_bytes']) for row in evidence
        if isinstance(row['memory_extra_peak_bytes'], (int, float))
        and not isinstance(row['memory_extra_peak_bytes'], bool)
        and math.isfinite(float(row['memory_extra_peak_bytes']))
        and float(row['memory_extra_peak_bytes']) >= 0
    ]
    pairs = [(r['memory_extra_peak_bytes'], r['host_extra_peak_bytes']) for r in evidence
             if all(isinstance(r.get(k), (int, float)) and not isinstance(r[k], bool)
                    and math.isfinite(r[k]) and r[k] >= 0
                    for k in ('memory_extra_peak_bytes', 'host_extra_peak_bytes'))]
    if not pairs:
        raise ValueError('training requires paired guest and host cgroup execution measurements')
    # A frozen empirical mapping, not an identity conversion or a hard bound.
    ratios = [host/guest for guest, host in pairs if guest > 0]
    scale = _percentile(ratios, .9) if ratios else 0.0
    offset = _percentile([max(0, host-scale*guest) for guest, host in pairs], .9)
    host_calibration = {'target': 'host_cgroup_memory_current_increment',
                        'method': 'training_p90_ratio_plus_p90_residual',
                        'sample_count': len(pairs), 'scale': scale, 'offset_bytes': offset}
    measured_p90_bytes = _percentile([host for _, host in pairs], .9)
    static_calibration = {
        'source': 'separate_validated_training_run',
        'metric': 'host_cgroup_memory_current_increment', 'quantile': .9,
        'sample_count': len(pairs), 'measured_bytes': measured_p90_bytes,
        'measured_mib': measured_p90_bytes / 1024**2,
        'recommended_mib': max(1, math.ceil(measured_p90_bytes / 1024**2)),
    }
    payload = {'schema': 'clawbox_prediction_v2', 'host_calibration': host_calibration, 'repository': repository, 'quantile': .5,
               'training_validated': True, 'training_validation': validations,
               'sandbox_identity': sandbox_identity,
               'clawtune_revision': _clawtune_revision(),
               'collector_semantics': COLLECTOR_SEMANTICS,
               'memory_measurement': 'guest_memtotal_minus_memavailable',
               'training_runs': [str(p.resolve()) for p in runs], 'source_sha256': digest.hexdigest(),
               'kb_sha256': hashlib.sha256(kb_bytes).hexdigest(),
               'trace_sha256': hashlib.sha256(trace.read_bytes()).hexdigest(),
               'training_calls': len(calls), 'training_clauses': len(clauses),
               'memory_labels': sum(r['memory_extra_peak_bytes'] is not None for r in evidence),
               'static_tool_memory_calibration': static_calibration,
               'selection_counts': dict(counts), 'unavailable': unavailable,
               'evidence': evidence, 'commands': records}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix('.kb.json').write_bytes(kb_bytes)
    output.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return {k: v for k, v in payload.items() if k not in {'evidence', 'commands'}}
