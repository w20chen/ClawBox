"""Train frozen P50 admission inputs from ClawBox CubeSandbox measurements."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from clawbox.replay.trace import load_trace
from clawbox.tuning.__main__ import find_run_datasets
from clawbox.tuning.dataset import build_joined_dataset
from clawbox.tuning.native import native_clause_observations
from clawbox.tuning.clawtune import observation_to_completed_call, predict_native_call_load_models
from clawbox.clawtune_integration import use_clawtune
from .prediction import command_sha256, select_p50, PredictionUnavailable
from .results import ResultEnvelope, RunStatus
from .spec import ExperimentSpec, load_experiment, spec_digest


@dataclass(frozen=True)
class TrainingArmSource:
    run_root: Path
    artifact_root: Path
    result: ResultEnvelope


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
        if result.arm.policy.name != 'tool-static-resident':
            raise ValueError(f'{run}: training accepts only tool-static-resident arms')

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
        identity = {k: getattr(spec.sandbox, k) for k in ('image_digest', 'vcpu', 'memory_mib', 'architecture')}
        if not identity['image_digest']:
            raise ValueError(f'{run}: missing immutable Tool image digest')
        if sandbox_identity is not None and identity != sandbox_identity:
            raise ValueError('Training runs use different Tool environments')
        sandbox_identity = identity
        for source in arm_sources:
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
                    if item.cgroup is None or not item.command or not item.complete or item.exit_code != 0:
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
                    call = observation_to_completed_call(item, repository)
                    calls.append(call)
                    short[item.command].append(raw)
                    memory = memory_labels(raw)
                    evidence.append({'execution_id': item.execution_id, 'command': item.command,
                                     'duration_ms': raw.get('duration_ms'),
                                     'memory_extra_peak_bytes': memory.get('memory_extra_peak_bytes'),
                                     'memory_unavailable_reason': raw.get('memory_unavailable_reason'),
                                     'observed_repository': item.repo_fingerprint, 'source': str(cgpath)})
                    if artpath.exists():
                        artifact = json.loads(artpath.read_text(encoding='utf-8'))
                        validate_artifact(artpath, artifact, expected_repo=repository)
                        digest.update(artpath.read_bytes())
                        for native in artifact.get('calls', []):
                            if native.get('tool_call_id') == item.execution_id and native.get('eligible_for_kb') is True:
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
    measured_p90_bytes = _percentile(valid_memory, .9)
    static_calibration = {
        'source': 'separate_validated_training_run',
        'metric': 'guest_memtotal_minus_memavailable_extra_peak',
        'quantile': .9,
        'sample_count': len(valid_memory),
        'measured_bytes': measured_p90_bytes,
        'measured_mib': measured_p90_bytes / (1024 * 1024),
        'recommended_mib': max(1, math.ceil(measured_p90_bytes / (1024 * 1024))),
    }
    payload = {'schema': 'clawbox_p50_v1', 'repository': repository, 'quantile': .5,
               'training_validated': True, 'training_validation': validations,
               'sandbox_identity': sandbox_identity,
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
