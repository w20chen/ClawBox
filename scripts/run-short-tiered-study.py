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
parser.add_argument('--steps', type=int, default=8)
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

spec = yaml.safe_load(args.spec.read_text())
source = Path(spec['workload']['input'])
records = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
if not all(r.get('type') == 'action' and r.get('action_type') == 'llm_call' for r in records):
    parser.error('this helper requires an action-format trace containing only llm_call records')
if args.steps >= len(records):
    parser.error('steps must leave a next recorded request for the explicit stop boundary')

root = args.output.resolve()
root.mkdir(parents=True)
(root / 'arms').mkdir()
(root / 'plans').mkdir()
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
    'recorded_tool_rounds': args.steps, 'synthetic_stop_responses': 1,
    'synthetic_stop_latency_seconds': 0,
    'interpretation': 'prefix execution and regression validation, not original task completion',
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
    log(f'Starting {index + 1}/{len(policies)}: {policy["name"]}, c40, {args.steps} recorded rounds')
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
            log(f'{policy["name"]}: 30-minute limit reached; stopping worker and cleaning its VMs')
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
        result = {
            'arm': {'policy': policy, 'concurrency': 40},
            'status': 'timed_out' if timed_out else 'failed',
            'started_at': started, 'completed_at': stamp(),
            'correctness': {'completed_sessions': 0, 'validation_passed': False,
                            'failure': f'Worker exit {code}; external timeout={timed_out}; inspect raw session logs'},
            'performance': {}, 'memory': {},
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
