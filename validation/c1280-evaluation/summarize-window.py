"""Read-only observation-window summary; unfinished sessions stay censored."""
import argparse
import collections
import json
import math
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('run', type=Path)
p.add_argument('--seconds', type=float, default=1200)
p.add_argument('--output', type=Path)
a = p.parse_args()
state = json.loads((a.run / 'run-state.json').read_text())
reports = []
for arm_id, arm in state['arms'].items():
    start = arm['started_unix_s']
    events = Path(arm['artifact_root']) / 'events' / (arm_id + '.jsonl')
    import yaml
    spec = yaml.safe_load((a.run / 'experiment.yaml').read_text())
    interval = spec['execution'].get('stagger_interval_seconds', 0)
    stagger = spec['execution'].get('arrival_schedule') == 'fixed_stagger'
    concurrency = max(spec['execution']['concurrency_levels'])
    counts = collections.Counter()
    waits = []
    successful_latencies = []
    peaks = {}
    scopes = collections.Counter()
    execution_scope = {}
    last = start
    failures = []
    created_sessions = set()
    first_memory_events = None
    last_memory_events = None
    max_psi = {}
    with events.open() as stream:
        for line in stream:
            try:
                e = json.loads(line)
                ts = int(e['wall_time_ns']) / 1e9
            except (ValueError, KeyError):
                continue
            if ts > start + a.seconds:
                break
            last = max(last, ts)
            kind = e.get('event')
            counts[kind] += 1
            if kind == 'sandbox_created':
                created_sessions.add(e.get('session_id'))
            if kind == 'tool_admitted':
                waits.append(e['admission_blocked_seconds'])
                execution_scope[(e['session_id'], e['execution_id'])] = e.get('execution_scope', 'unspecified')
            if kind == 'tool_completed':
                scopes[execution_scope.get((e['session_id'], e['execution_id']), 'unmatched')] += 1
            if kind == 'session_complete' and e.get('valid') is True:
                index = int(e['session_id'].rsplit('-', 1)[1])
                successful_latencies.append(ts - start - (index * interval if stagger else 0))
            if kind == 'session_failed' and len(failures) < 5:
                failures.append(e)
            if kind == 'memory_sample':
                cg = e.get('cgroup_memory') or {}
                if isinstance(cg.get('events'), dict):
                    if first_memory_events is None:
                        first_memory_events = cg['events']
                    last_memory_events = cg['events']
                for category, observation in (cg.get('pressure') or {}).items():
                    if isinstance(observation, dict) and isinstance(observation.get('avg10'), (int, float)):
                        max_psi[category] = max(max_psi.get(category, 0), observation['avg10'])
                for k in ('local_used_bytes', 'combined_live_used_bytes', 'shared_live_used_bytes',
                          'warm_allocated_bytes', 'warm_reserved_bytes', 'warm_committed_bytes', 'shared_borrow_reserved_bytes'):
                    v = e.get(k)
                    if isinstance(v, (int, float)):
                        peaks[k] = max(peaks.get(k, 0), v)
    duration = min(a.seconds, max(0, last - start))
    offered = min(concurrency, math.floor(duration / interval) + 1) if stagger and interval else concurrency
    def quantiles(values):
        if not values:
            return None
        s = sorted(values)
        return {k: s[min(len(s)-1, math.ceil(q * len(s))-1)] for k, q in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]}
    reports.append(dict(arm_id=arm_id, run_state=state['state'], cleanup_verified=arm.get('cleanup_verified'),
        requested_window_seconds=a.seconds, observed_window_seconds=duration,
        offered_sessions=offered, target_sessions=concurrency, sessions_with_vm_created=len(created_sessions),
        successful_agents=len(successful_latencies), successful_agents_per_second=len(successful_latencies)/duration if duration else None,
        successful_fraction_of_offered=len(successful_latencies)/offered if offered else None,
        successful_latency_seconds_conditional_on_completion=quantiles(successful_latencies),
        latency_note='Completion-conditioned percentiles exclude unfinished requests; no all-request P95/P99 claim.',
        admission_wait_seconds_completed_admissions=quantiles(waits),
        completed_execution_scopes=dict(scopes), event_counts=dict(counts), memory_peaks_bytes=peaks,
        cgroup_memory_event_deltas=({k: last_memory_events.get(k, 0)-v for k, v in first_memory_events.items()}
                                    if first_memory_events is not None and last_memory_events is not None else None),
        cgroup_memory_psi_avg10_max=max_psi,
        failure_examples=failures, source_events=str(events)))
out = json.dumps(reports, indent=2)
if a.output:
    a.output.write_text(out + '\n')
print(out)
