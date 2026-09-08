#!/usr/bin/env python3
"""Run a fixed replay prefix at c40, with a wall-clock limit for each baseline.

Run from an installed ClawBox checkout with the machine environment loaded.
The original trace is preserved. A clearly labeled synthetic stop response
consumes the next recorded request after the selected tool rounds finish.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clawbox.cube.client import CubeSandboxClient
from clawbox.experiments.spec import ExperimentSpec

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--spec', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--steps', type=int, default=23)
parser.add_argument('--full-trace', action='store_true', help='Replay every original row without a synthetic boundary')
parser.add_argument('--arm-seconds', type=int, default=1800)
args = parser.parse_args()
if args.steps < 1 or args.arm_seconds < 1:
    parser.error('steps and arm-seconds must be positive')
if args.output.exists():
    parser.error('output already exists; use a fresh run directory')

def stamp():
    return datetime.now(timezone.utc).isoformat()

def log(message):
    print(stamp(), message, flush=True)

def recover_partial(run_directory):
    """Keep observed progress and memory even when the worker was interrupted."""
    completed = set()
    pauses = restores = 0
    samples = []
    for path in (run_directory / 'events').glob('*.jsonl'):
        for line in path.open():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # the final write may have been interrupted
            event = row.get('event')
            if event == 'session_complete' and row.get('valid'):
                completed.add(row['session_id'])
            pauses += event == 'sandbox_paused'
            restores += event == 'sandbox_restored'
            if event == 'memory_sample' and row.get('local_used_bytes') is not None:
                samples.append((int(row['monotonic_time_ns']) / 1e9, row['local_used_bytes']))
    samples.sort()
    memory = {}
    if samples:
        memory['peak_used_delta_bytes'] = max(value for _, value in samples)
        memory['memory_time_integral_byte_seconds'] = sum(
            (b[0] - a[0]) * (a[1] + b[1]) / 2 for a, b in zip(samples, samples[1:]))
    return ({'completed_sessions': len(completed), 'validation_passed': False,
             'partial_observations': True},
            {'pause_count': pauses, 'resume_count': restores}, memory)

spec = yaml.safe_load(args.spec.read_text())
resources = spec['resources']
local_group = Path(resources['local_memory_cgroup'])
expected_limit = resources['local_memory_capacity_mib'] * 1024**2
if (local_group / 'memory.max').read_text().strip() != str(expected_limit):
    parser.error('LOCAL limit is not configured; run scripts/setup-tiered-memory.sh first')
warm_type = subprocess.check_output([
    'findmnt', '-n', '-o', 'FSTYPE', '--target', resources['warm_snapshot_root']], text=True).strip()
if warm_type != 'tmpfs':
    parser.error('WARM tmpfs is not mounted; run scripts/setup-tiered-memory.sh first')
source = Path(spec['workload']['input'])
records = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
if not all(r.get('type') == 'action' and r.get('action_type') == 'llm_call' for r in records):
    parser.error('this helper requires an action-format trace containing only llm_call records')
if not args.full_trace and args.steps >= len(records):
    parser.error('steps must leave a next recorded request for the explicit stop boundary')

root = args.output.resolve()
root.mkdir(parents=True)
(root / 'arms').mkdir()
(root / 'plans').mkdir()
if args.full_trace:
    trace = root / 'replay-full.jsonl'
    shutil.copyfile(source, trace)
else:
    prefix = copy.deepcopy(records[:args.steps + 1])
    stop = prefix[-1]
    stop['data']['raw_response'] = {
        'content': 'Replay workload prefix finished at the configured experiment boundary.',
        'tool_calls': [],
    }
    stop['data']['llm_latency_ms'] = 0
    stop['ts_end'] = stop['ts_start']
    stop['data']['clawbox_synthetic_boundary'] = True
    trace = root / 'replay-prefix.jsonl'
    trace.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in prefix))
spec['workload']['input'] = str(trace)
for case in spec['workload']['cases']:
    case['replay_trace_reference'] = str(trace)
spec['execution'].update(concurrency_levels=[40], arm_timeout_seconds=args.arm_seconds,
                         randomized_order=False)
spec['workload']['repetitions'] = 1
policies = list(spec['policies'])
random.Random(spec['execution'].get('random_seed', 20260907)).shuffle(policies)
provenance = {
    'source_trace': str(source), 'original_model_steps': len(records),
    'recorded_tool_rounds': len(records) if args.full_trace else args.steps,
    'synthetic_stop_responses': 0 if args.full_trace else 1,
    'synthetic_stop_latency_seconds': 0,
    'interpretation': ('full recorded trace and regression validation' if args.full_trace else
                       'prefix execution and regression validation, not original task completion'),
    'concurrency': 40, 'per_arm_wall_seconds': args.arm_seconds,
    'policy_order': [p['name'] for p in policies],
}
(root / 'prefix-provenance.json').write_text(json.dumps(provenance, indent=2))
results = []
client = CubeSandboxClient()
for index, policy in enumerate(policies):
    run_id = f'{root.name}-{index:02d}'
    arm_spec = copy.deepcopy(spec)
    arm_spec['experiment_id'] = root.name
    arm_spec['policies'] = [policy]
    ExperimentSpec.model_validate(arm_spec)
    plan = root / 'plans' / f'{index:02d}.yaml'
    plan.write_text(yaml.safe_dump(arm_spec, sort_keys=False))
    environment = dict(os.environ, CLAWBOX_OUTPUT_ROOT=str(root / 'runs'), PYTHONUNBUFFERED='1')
    log(f'Starting {index + 1}/{len(policies)}: {policy["name"]}, c40, {provenance["recorded_tool_rounds"]} recorded rounds')
    started = stamp()
    timed_out = False
    with (root / f'{index:02d}.log').open('w') as output:
        process = subprocess.Popen([
            sys.executable, '-m', 'clawbox.experiments.worker', '--spec', str(plan),
            '--run-id', run_id, '--attempt-id', run_id, '--task-uid', run_id,
        ], env=environment, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=args.arm_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            log(f'{policy["name"]}: {args.arm_seconds}-second limit reached; stopping worker and cleaning its VMs')
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            code = process.returncode
    # Cleanup must finish before another baseline starts, including after timeout.
    client.kill_owned_sandboxes(run_id)
    completed = list((root / 'runs' / run_id / 'arms').glob('*.json'))
    if completed:
        result = json.loads(completed[0].read_text())
    else:
        partial_correctness, partial_performance, partial_memory = recover_partial(root / 'runs' / run_id)
        result = {
            'arm': {'policy': policy, 'concurrency': 40},
            'status': 'timed_out' if timed_out else 'failed',
            'started_at': started, 'completed_at': stamp(),
            'correctness': {**partial_correctness,
                            'failure': f'Worker exit {code}; external timeout={timed_out}; inspect raw session logs'},
            'performance': partial_performance, 'memory': partial_memory,
        }
    if timed_out:
        result['status'] = 'timed_out'
        result['correctness']['validation_passed'] = False
    results.append(result)
    (root / 'arms' / f'{index:02d}.json').write_text(json.dumps(result, indent=2))
    (root / 'summary.json').write_text(json.dumps({'arms': results, 'prefix': provenance}, indent=2))
    with (root / 'report.md').open('w') as output:
        subprocess.run([sys.executable, str(Path(__file__).with_name('report-standalone-study.py')),
                        str(root)], stdout=output, check=True)
    log(f'Finished {index + 1}/{len(policies)}: {policy["name"]}: {result["status"]}')
log(f'Study finished. Report: {root / "report.md"}')
