"""Train frozen P50 admission inputs from ClawBox CubeSandbox measurements."""
from __future__ import annotations

import hashlib
import json
import yaml
from collections import Counter, defaultdict
from pathlib import Path

from clawbox.replay.trace import load_trace
from clawbox.tuning.__main__ import find_run_datasets
from clawbox.tuning.dataset import build_joined_dataset
from clawbox.tuning.native import native_clause_observations
from clawbox.tuning.clawtune import observation_to_completed_call, predict_native_call_load_models
from clawbox.clawtune_integration import use_clawtune
from .prediction import command_sha256, select_p50, PredictionUnavailable


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
        # Training sources must be managed Cube runs, not Docker benchmark traces.
        if not (run / 'owned-sandboxes.jsonl').is_file():
            raise ValueError(f'{run}: expected a ClawBox CubeSandbox run')
        spec = yaml.safe_load((run / 'experiment.yaml').read_text(encoding='utf-8'))
        if not (spec.get('validation') or {}).get('command'):
            raise ValueError(f'{run}: training requires an explicit final validation.command')
        summary = json.loads((run / 'summary.json').read_text(encoding='utf-8'))
        if not summary.get('arms') or any(a.get('status') != 'succeeded' for a in summary['arms']):
            raise ValueError(f'{run}: training requires a completed, successful validation run')
        identity = {k: spec['sandbox'].get(k) for k in ('image_digest', 'vcpu', 'memory_mib', 'architecture')}
        if not identity['image_digest']:
            raise ValueError(f'{run}: missing immutable Tool image digest')
        if sandbox_identity is not None and identity != sandbox_identity:
            raise ValueError('Training runs use different Tool environments')
        sandbox_identity = identity
        validations.append({'run': str(run), 'command': spec['validation']['command'], 'status': 'succeeded'})
        repos = {c.get('repository') for c in spec['workload']['cases']}
        if repos != {repository}:
            raise ValueError(f'{run}: training run must contain only repository {repository}')
        for traces, bridge, resources in find_run_datasets(run):
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
    payload = {'schema': 'clawbox_p50_v1', 'repository': repository, 'quantile': .5,
               'training_validated': True, 'training_validation': validations,
               'sandbox_identity': sandbox_identity,
               'memory_measurement': 'guest_memtotal_minus_memavailable',
               'training_runs': [str(p.resolve()) for p in runs], 'source_sha256': digest.hexdigest(),
               'kb_sha256': hashlib.sha256(kb_bytes).hexdigest(),
               'trace_sha256': hashlib.sha256(trace.read_bytes()).hexdigest(),
               'training_calls': len(calls), 'training_clauses': len(clauses),
               'memory_labels': sum(r['memory_extra_peak_bytes'] is not None for r in evidence),
               'selection_counts': dict(counts), 'unavailable': unavailable,
               'evidence': evidence, 'commands': records}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix('.kb.json').write_bytes(kb_bytes)
    output.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return {k: v for k, v in payload.items() if k not in {'evidence', 'commands'}}
