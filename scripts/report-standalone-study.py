#!/usr/bin/env python3
"""Print a compact research summary from completed standalone experiment arms."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('directory', type=Path)
args = parser.parse_args()

def number(value):
    return 'n/a' if value is None else f'{value:.2f}'

print(f'# Standalone study: {args.directory.name}\n')
print('| Policy | c | Status | Valid sessions | JCT p50/p95 s | Agents/min | LOCAL peak GiB | LOCAL GiB-s | Pauses/restores | ID join | Lost events |')
print('|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|')
failures = []
count = 0
for path in sorted((args.directory / 'arms').glob('*.json')):
    result = json.loads(path.read_text())
    arm = result['arm']
    correctness = result['correctness']
    performance = result['performance']
    memory = result['memory']
    gib = 1024 ** 3
    cells = [
        arm['policy']['name'], str(arm['concurrency']), result['status'],
        f"{correctness.get('completed_sessions', 0)}/{arm['concurrency']}",
        f"{number(performance.get('jct_p50_seconds'))}/{number(performance.get('jct_p95_seconds'))}",
        number(performance.get('agents_per_minute')),
        number(memory.get('peak_used_delta_bytes', 0) / gib),
        number(memory.get('memory_time_integral_byte_seconds', 0) / gib),
        f"{performance.get('pause_count', 0)}/{performance.get('resume_count', 0)}",
        number(correctness.get('native_tool_exact_id_join_rate')),
        str(correctness.get('native_tool_telemetry_loss_total', 'n/a')),
    ]
    print('| ' + ' | '.join(cells) + ' |')
    count += 1
    if correctness.get('failure'):
        failures.append(f"- {arm['policy']['name']}: {correctness['failure']}")
print(f'\nCompleted arm records: {count}.')
print('\nLOCAL columns require the scoped LOCAL cgroup sampler configured in the study YAML. '
      'Failed arms are not valid performance comparisons. A single repetition does not '
      'estimate run-to-run uncertainty. WARM gives the two tiered policies additional '
      'capacity; these results do not isolate policy benefit from capacity benefit.')
if failures:
    print('\nFailures:\n\n' + '\n'.join(failures))
