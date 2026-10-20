#!/usr/bin/env python3
"""Serial paper-study orchestration through the public experiment CLI only.

Plan first; --execute waits for the reference, freezes measured choices, and
runs each new ID at most once. No failed experiment is automatically rerun.
"""
from __future__ import annotations

import argparse
import collections
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TERMINAL = {'succeeded', 'failed', 'aborted', 'destroyed'}
SHARED_KINDS = {f'{name}-{scale}' for scale in (2560, 4096)
                for name in ('no-pool', 'no-borrow', 'pool-low', 'pool-mid')}


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def q90(values):
    return sorted(values)[math.ceil(.9 * len(values)) - 1] if values else None


def analyze(run):
    """Success rates retain failed/censored tasks; no completed-only population P90."""
    state = read(run / 'run-state.json')
    spec = yaml.safe_load((run / 'experiment.yaml').read_text())
    count = max(spec['execution']['concurrency_levels'])
    counts = collections.Counter()
    event_times = collections.defaultdict(list)
    scopes, tools, arrivals, completed, models, timings = {}, {}, {}, {}, {}, {}
    samples, peaks = [], collections.Counter()
    first = last = last_creation = None
    files = list(run.glob('attempts/*/*/events/*.jsonl'))
    if len(files) != 1:
        raise RuntimeError(f'{run.name}: expected one attempt/event file, found {len(files)}')
    for line in files[0].open():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        t = int(row['wall_time_ns']) / 1e9
        first = t if first is None else min(first, t)
        last = t
        kind, sid = row['event'], row.get('session_id')
        key = sid, row.get('execution_id')
        counts[kind] += 1
        event_times[kind].append(t)
        if kind == 'sandbox_create_gate_wait_started':
            arrivals[sid] = t
        elif kind == 'sandbox_created':
            last_creation = t
        elif kind == 'tool_admitted':
            scopes[key] = row.get('execution_scope')
        elif kind == 'tool_completed' and scopes.get(key) == 'agent-tool':
            tools[key] = t
        elif kind == 'session_complete' and row.get('valid'):
            completed[sid] = t
        elif kind == 'session_timing':
            timings[sid] = row.get('time_spans', [])
        elif kind == 'memory_sample':
            fields = ['warm_allocated_bytes', 'shared_live_used_bytes', 'warm_reserved_bytes',
                      'warm_committed_bytes', 'shared_borrow_reserved_bytes', 'combined_live_used_bytes']
            for f in fields:
                peaks[f] = max(peaks[f], row.get(f) or 0)
            physical = (row.get('warm_allocated_bytes') or 0) + (row.get('shared_live_used_bytes') or 0)
            accounted = sum(row.get(f) or 0 for f in ('warm_reserved_bytes', 'warm_committed_bytes', 'shared_borrow_reserved_bytes'))
            peaks['shared_joint_physical_bytes'] = max(peaks['shared_joint_physical_bytes'], physical)
            peaks['shared_joint_accounted_bytes'] = max(peaks['shared_joint_accounted_bytes'], accounted)
            pressure = row.get('compute_pressure') or {}
            samples.append((t, sum(n.get('admission_queue_depth', 0) for n in pressure.values()),
                            sum(n.get('running_vms', 0) for n in pressure.values())))
    artifact = files[0].parent.parent
    for path in artifact.glob('model-gateway/*.json'):
        if '.prefix-stop.' in path.name or '.rejected-request-' in path.name:
            continue
        rows = read(path)
        if isinstance(rows, list):
            for r in rows:
                if r.get('delivered') and r.get('status_code') == 200 and r.get('delivered_unix_s'):
                    models[path.name, r['request_id']] = r['delivered_unix_s']
    for sid in completed:
        for span in timings.get(sid, []):
            if span.get('name') == 'agent':
                completed[sid] = span['end_unix_s']
    for path in artifact.glob('model-gateway/*.deliveries.jsonl'):
        session_file = path.name.removesuffix('.deliveries.jsonl') + '.json'
        for line in path.open():
            try:
                r = json.loads(line)
            except ValueError:
                continue  # A concurrent append can leave the last line incomplete.
            if r.get('status_code') == 200 and r.get('delivered_unix_s'):
                models[session_file, r['request_id']] = r['delivered_unix_s']
    results = [read(p) for p in (run / 'arms').glob('*.json')]
    origins = []
    for result in results:
        for timeline in result.get('performance', {}).get('session_timelines', []):
            if not timeline:
                continue
            start, offset = timeline.get('offered_start_unix_s'), timeline.get('offered_start_offset_seconds')
            if start is not None and offset is not None:
                origins.append(start - offset)
    # Successful finalization completions can be absent from arm timelines.
    # Join raw events, and never substitute validation/cleanup time for agent end.
    latencies = []
    for sid in completed:
        ends = [s['end_unix_s'] for s in timings.get(sid, [])
                if s.get('name') == 'agent' and s.get('end_unix_s') is not None]
        if sid in arrivals and ends and ends[-1] >= arrivals[sid]:
            latencies.append(ends[-1] - arrivals[sid])
    origin = min(origins) if origins else min(arrivals.values(), default=first)
    if origin is None:
        raise RuntimeError(f'{run.name}: no measurement events')
    end = min(last, origin + spec['execution']['arm_timeout_seconds'])
    if state['state'] == 'succeeded' and completed:
        end = max(completed.values())
    good = {sid:t for sid,t in completed.items() if origin <= t <= end}
    def window(a, b):
        seconds = max(.001, b - a)
        def contains(t):
            return a <= t < b or (a <= t == b == end)
        agents = sum(contains(t) for t in good.values())
        steps = sum(contains(t) for t in tools.values()) + sum(contains(t) for t in models.values())
        qs = [q for t,q,v in samples if a <= t < b]
        return dict(start_s=a-origin, end_s=b-origin, agents=agents,
                    agents_per_min=agents*60/seconds, steps_per_s=steps/seconds,
                    queue_sample_min=min(qs) if qs else None, queue_sample_max=max(qs) if qs else None)
    bins = [window(origin+i*120, origin+(i+1)*120) for i in range(int((end-origin)//120))]
    latest = bins[-3:]
    rates = [b['steps_per_s'] for b in latest]
    stable = len(rates) == 3 and statistics.fmean(rates) > 0 and statistics.pstdev(rates)/statistics.fmean(rates) <= .15
    stable = stable and all((b['queue_sample_min'] or 0) > 0 for b in latest)
    performance = results[0].get('performance', {}) if results else {}
    correctness = results[0].get('correctness', {}) if results else {}
    triggers = {k:performance.get(k) for k in ('pause_count', 'resume_count', 'shared_memory_borrow_count',
                'shared_memory_return_count', 'local_high_crossings', 'template_affinity_decisions',
                'template_cache_hits', 'runtime_memory_control', 'tool_call_cpu_policy')}
    for value in triggers.values():
        if isinstance(value, dict):
            value.pop('events', None)
    duration = max(.001, end-origin)
    arrival_span = max(arrivals.values())-min(arrivals.values()) if len(arrivals)>1 else None
    return dict(run_id=run.name, state=state['state'], offered=count, measurement_seconds=duration,
                successful_agents=len(good), success_fraction=len(good)/count,
                actual_arrivals=len(arrivals), actual_arrival_rate_per_s=(len(arrivals)-1)/arrival_span if arrival_span else None,
                seconds_since_last_creation=end-last_creation if last_creation is not None else None,
                successful_agents_per_min=len(good)*60/duration,
                whole_window=window(origin, end), last_300s=window(max(origin,end-300),end), bins_120s=bins,
                stable_last_360s=stable, completed_only_p90_s=q90(latencies),
                latency_samples=len(latencies), successful_including_finalization=len(completed),
                latency_definition='Actual create-gate arrival to agent end; successful tasks including finalization; excludes validation/cleanup',
                all_tasks_successful=len(good)==count,
                population_p90_s=q90(latencies) if len(completed)==count and len(latencies)==count else None,
                censored_or_failed=count-len(good), peaks_bytes=dict(peaks), event_counts=dict(counts),
                measurement_event_counts={k: sum(origin <= t <= end for t in times)
                                          for k, times in event_times.items()},
                event_counts_scope='Full event log including finalization; measurement_event_counts excludes shutdown',
                mechanisms=triggers, peak_running_vms=max((v for t,q,v in samples),default=0),
                failure_category=results[0].get('failure_category') if results else 'missing_result',
                telemetry_loss=correctness.get('native_tool_telemetry_loss_total'),
                cleanup_verified=all(a.get('cleanup_verified') for a in state.get('arms',{}).values()),
                spec_sha256=hashlib.sha256((run/'experiment.yaml').read_bytes()).hexdigest())


def measured_summary(run, out):
    final = analyze(run)
    stable = out/(run.name+'.stable-stop.json')
    if not stable.exists():
        return final
    snapshot = read(stable)
    # Shutdown/cleanup is not part of the steady window which triggered stop.
    snapshot['finalization'] = {k: final.get(k) for k in (
        'state','cleanup_verified','failure_category','event_counts')}
    snapshot['state'] = final['state']
    snapshot['cleanup_verified'] = final['cleanup_verified']
    snapshot['measurement_stop_reason'] = 'stable_throughput'
    snapshot['mechanisms'] = final['mechanisms']
    return snapshot


def capacity_points(reference, full_gib):
    peaks = reference['peaks_bytes']
    required = max(peaks.get('shared_joint_physical_bytes',0), peaks.get('shared_joint_accounted_bytes',0),
                   2*peaks.get('shared_live_used_bytes',0), 2*peaks.get('shared_borrow_reserved_bytes',0))/2**30
    if required < 1:
        return [], 'Reference used less than 1 GiB of shared capacity; capacity sweep deferred as uninformative.'
    sizes = sorted({max(16, min(full_gib-16, math.ceil(required*f/16)*16)) for f in (.5, .8)})
    if len(sizes) < 2:
        sizes = sorted({16, min(full_gib-16, max(32, sizes[0]+16))})
    return sizes, f'Envelope max(joint physical, joint accounted, 2*live, 2*reserved borrow)={required:.3f} GiB; 50% borrow fraction retained.'


def shared_pool_trigger(reference):
    """Require material use before spending time on a shared-pool ablation."""
    peaks = reference['peaks_bytes']
    checkpoint = max(peaks.get('warm_allocated_bytes', 0),
                     peaks.get('warm_reserved_bytes', 0),
                     peaks.get('warm_committed_bytes', 0))
    borrow = peaks.get('shared_borrow_reserved_bytes', 0)
    live = peaks.get('shared_live_used_bytes', 0)
    triggered = checkpoint > 0 or borrow > 0 or live >= 2**30
    reason = (f'Full-2560 peaks: checkpoint={checkpoint/2**30:.3f} GiB, '
              f'borrow={borrow/2**30:.3f} GiB, shared-live={live/2**30:.3f} GiB.')
    return triggered, borrow > 0, reason


def measured_service_rate(reference):
    bins = reference['bins_120s']
    tail = bins[-5:]
    n = sum(b['agents'] for b in tail)
    if n >= 20:
        return n / sum(b['end_s']-b['start_s'] for b in tail), 'last up to ten minutes, >=20 successful completions; measured anchor, not proven capacity'
    if reference['successful_agents'] >= 20:
        return reference['successful_agents']/reference['measurement_seconds'], 'whole-run successful completion rate; startup-inclusive conservative anchor'
    return None, 'Fewer than 20 successful completions: insufficient evidence to choose a success-rate sweep.'


def prioritize_latency_points(plan):
    if not plan.get('prefer_low_scale_before_escalation'):
        return
    for row in plan['order']:
        if row['kind'].endswith('-4096') and row['state'] == 'pending':
            row.update(state='deferred', reason='User deferred 4096 pending low-concurrency latency and repair evidence.')
    low = {'full-c128', 'ledger-c128', 'full-c64', 'ledger-c64',
           'rate64-light', 'rate64-saturation', 'rate64-overload'}
    plan['order'].sort(key=lambda row: (
        0 if row['kind'] in {'full-2560-before-repair', 'full-2560'} else
        1 if row['kind'] in low else 2))
    plan['rules']['execution_order'] = [
        'Finish active Full-2560', 'Full/ledger c128 and c64',
        'Full c64 latency-rate scan', 'conditional shared-pool ablations',
        '4096 deferred for review, not automatically launched']


def stable_stop_allowed(snapshot):
    ready = snapshot['event_counts'].get('task_environment_checked', 0)
    errors = snapshot['event_counts'].get('session_failed', 0)
    return (snapshot['measurement_seconds'] >= 600
            and snapshot['actual_arrivals'] == snapshot['offered']
            and (snapshot['seconds_since_last_creation'] or 0) >= 360
            and snapshot['stable_last_360s']
            and ready > 0 and errors <= .05*ready)


def configure_pressure_plan(plan):
    if not plan.get('pressure640_fast_plan'):
        return
    plan['hold_new_runs_for_control_repair'] = False
    plan['prefer_low_scale_before_escalation'] = False
    plan.setdefault('deadline_unix_s', time.time()+6600)
    for row in plan['order']:
        if row['state'] == 'pending' and not row['kind'].startswith('pressure640-'):
            row.update(state='deferred', reason='User prioritized reduced-LOCAL 640 ablations within two hours')
    known = {r['kind'] for r in plan['order']}
    for variant in ('full','no-borrow','no-checkpoint','no-runtime','round-robin','no-llc','no-pool'):
        kind = 'pressure640-'+variant
        if kind not in known:
            plan['order'].append(dict(kind=kind, tasks=640, state='pending',
                run_id='sqlglot-study-'+kind+'-01'))
    plan['rules']['pressure640'] = {
        'local_gib':448,'shared_pool_gib':320,'borrow_limit_gib':160,'cpu_cores':160,
        'evidence':'Full640 late-window physical mean 633.15 GiB, P90 652.25 GiB, peak 656.49 GiB; no borrowing',
        'point_limit_seconds':900, 'no_automatic_reruns':True,
        'round_robin_caveat':'Disables load-aware selection and template affinity, retains NUMA binding and C2',
        'no_pool_caveat':'Disables borrowing and RAM checkpoint tier; retains disk checkpoint with LRU',
        'verification':'Full128 currently healthy; first pressure640 control is also the high-density verification, not proof in advance'}


def make_spec(base, kind, value=None):
    if kind.startswith('pressure640-'):
        d = make_spec(base, 'full-640-control')
        d['comparison']['arms'] = [{'name': kind}]
        r, m, x = d['environment']['resources'], d['mechanisms'], d['execution']
        if len(r['compute_nodes']) != 2:
            raise ValueError('This measured study budget requires the verified two LOCAL domains')
        for node in r['compute_nodes']:
            node.update(memory_capacity_mib=224*1024, low_watermark_mib=200*1024,
                        high_watermark_mib=215*1024)
        r.update(local_memory_capacity_mib=448*1024, pool_memory_budget_mib=430*1024,
                 local_memory_low_watermark_mib=400*1024,
                 local_memory_high_watermark_mib=430*1024)
        x.update(arm_timeout_seconds=900, arm_finalization_timeout_seconds=120,
                 worker_thread_switch_interval_seconds=.001)
        variant = kind.removeprefix('pressure640-')
        if variant == 'no-borrow':
            m['shared_memory']['borrow'] = 'off'
        elif variant == 'no-checkpoint':
            m['idle_reclaim']['policy'] = 'off'
        elif variant == 'no-runtime':
            m['runtime'].update(memory_protection=False, cpu_release_boost=False,
                                proactive_memory_reclaim=False)
        elif variant == 'round-robin':
            m['placement'].update(policy='round_robin', template_affinity=False)
        elif variant == 'no-llc':
            m['placement']['tool_call_cpu_policy'] = 'c0'
        elif variant == 'no-pool':
            r.update(warm_memory_capacity_mib=0, shared_memory_borrow_limit_mib=None,
                     warm_snapshot_root=None, warm_numa_node=None)
            m['shared_memory']['borrow'] = 'off'
            m['checkpoint'].update(storage='disk', residency='lru')
        elif variant != 'full':
            raise ValueError(f'unknown pressure-study variant: {variant}')
        return d
    scale = 4096 if kind.endswith('-4096') else 2560
    kind = kind.removesuffix('-4096')+'-2560' if scale == 4096 else kind
    d = deepcopy(base)
    r, m, x = d['environment']['resources'], d['mechanisms'], d['execution']
    d['comparison'] = {'recipe':'custom', 'arms':[{'name':kind}]}
    if kind == 'no-pool-2560':
        r.update(warm_memory_capacity_mib=0, shared_memory_borrow_limit_mib=None, warm_snapshot_root=None, warm_numa_node=None)
        m['shared_memory']['borrow'] = 'off'
        m['checkpoint'].update(storage='disk', residency='lru')
    elif kind == 'no-borrow-2560':
        m['shared_memory']['borrow'] = 'off'
    elif kind.startswith('pool-') and kind.endswith('-2560'):
        r.update(warm_memory_capacity_mib=value*1024, shared_memory_borrow_limit_mib=value*512)
    elif kind == 'full-2560':
        x.update(concurrency_levels=[2560], arm_timeout_seconds=2400)
    elif kind in ('full-c128', 'ledger-c128'):
        x.update(concurrency_levels=[128], arm_timeout_seconds=3000)
    elif kind in ('full-c64', 'ledger-c64'):
        x.update(concurrency_levels=[64], arm_timeout_seconds=3600)
    elif kind.startswith('rate64-'):
        x.update(concurrency_levels=[64], poisson_arrival_window_fraction=value)
    elif kind.startswith('rate-'):
        x['poisson_arrival_window_fraction'] = value
    elif kind in ('full-640-control', 'no-llc', 'round-robin', 'no-runtime'):
        x['concurrency_levels'] = [640]
        if kind == 'no-llc':
            m['placement']['tool_call_cpu_policy'] = 'c0'
        elif kind == 'round-robin':
            m['placement'].update(policy='round_robin', template_affinity=False)
        elif kind == 'no-runtime':
            m['runtime'].update(memory_protection=False, cpu_release_boost=False, proactive_memory_reclaim=False)
    if kind in SHARED_KINDS:
        x.update(concurrency_levels=[2560], arm_timeout_seconds=2400)
    if scale == 4096:
        x.update(concurrency_levels=[4096], arm_timeout_seconds=3600)
    return d


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-spec', type=Path, required=True)
    parser.add_argument('--ledger-spec', type=Path, required=True)
    parser.add_argument('--host', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--project-root', type=Path, default=ROOT,
                        help='ClawBox checkout containing scripts/clawbox (needed when this runner is staged elsewhere)')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    ROOT = args.project_root.resolve()
    sys.path.insert(0, str(ROOT))
    out = args.output.resolve(); out.mkdir(parents=True,exist_ok=True)
    base = yaml.safe_load(args.base_spec.read_text())
    ledger_base = yaml.safe_load(args.ledger_spec.read_text())
    host = yaml.safe_load(args.host.read_text())
    plan_path = out/'matrix.json'
    if not plan_path.exists():
        rows = [('full-2560',2560),
                ('no-pool-2560',2560),('no-borrow-2560',2560),
                ('pool-low-2560',2560),('pool-mid-2560',2560),
                ('full-c128',128),('ledger-c128',128),('full-c64',64),('ledger-c64',64),
                ('rate64-light',64),('rate64-saturation',64),('rate64-overload',64)]
        save(plan_path, {'reference':str(args.reference.resolve()), 'state':'planned', 'order':[
            {'kind':k,'tasks':n,'state':'pending','run_id':'sqlglot-study-'+k+'-01'} for k,n in rows],
            'rules':{'retries':0,'create_concurrency':base['execution']['sandbox_create_concurrency'],
                     'same_workload_seed_and_images':True,'throughput_arm_seconds':base['execution']['arm_timeout_seconds'],
                     'no_pool_changes':['live borrowing off','checkpoint RAM tier removed; disk incremental checkpoints retained','value tier residency inapplicable; disk-only lru'],
                     'round_robin_scope':'disable load-aware/template-affinity decisions; keep NUMA binding, LLC and borrowing',
                     'execution_order':['Full-1280 reference','Full-2560','conditional Full-2560 shared-pool ablations',
                                        'Full/ledger c128 and c64','Full c64 latency-rate scan'],
                     'shared_pool_rule':'run at c2560 only when Full-2560 materially uses checkpoint or borrowing; otherwise defer',
                     'full_1280_rate_sweep':'cancelled: completion time is not practical',
                     'rate64_factors':[.5,1.0,1.5],
                     'rate_timeout':'realized last arrival + max(1800s, 2*Full-c64 completed-only P90)',
                     'p90_rule':'population P90 only when all 64 tasks complete; otherwise completed-only is labelled censored'}})
    if not args.execute:
        print(plan_path); return
    import fcntl
    lock = (out/'runner.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    plan = read(plan_path)
    if not any(row['kind'] == 'full-4096' for row in plan['order']):
        position = next(i for i, row in enumerate(plan['order']) if row['kind'] == 'full-2560') + 1
        plan['order'][position:position] = [
            {'kind': kind, 'tasks': 4096, 'state': 'pending',
             'run_id': 'sqlglot-study-'+kind+'-01'}
            for kind in ('full-4096', 'no-pool-4096', 'no-borrow-4096',
                         'pool-low-4096', 'pool-mid-4096')]
        plan['rules']['checkpoint_scale_escalation'] = (
            'If Full-2560 has no sandbox_paused event, run Full-4096 for up to 60 minutes; '
            'use only the matching scale for subsequent shared-pool ablations.')
    prioritize_latency_points(plan)
    configure_pressure_plan(plan)
    while True:
        statepath = args.reference/'run-state.json'
        if statepath.exists() and read(statepath)['state'] in TERMINAL:
            break
        plan['state']='waiting_for_reference'; save(plan_path,plan); time.sleep(30)
    ref = analyze(args.reference); save(out/'reference-summary.json',ref)
    sizes, reason = [], 'Awaiting Full-2560 shared-pool measurements.'
    plan.update(state='running', capacity_sizes_gib=sizes, capacity_choice=reason,
                full_1280_shared_pool_ablation='cancelled: no material shared-pool use',
                full_1280_rate_sweep='cancelled')
    save(plan_path,plan)
    from clawbox.experiments.spec import load_experiment, expand_matrix
    from clawbox.experiments.worker import session_offer_offsets
    def cli(argv, log):
        with log.open('a') as stream:
            return subprocess.run(['bash',str(ROOT/'scripts/clawbox'),'--sudo',*argv],cwd=ROOT,
                                  stdout=stream,stderr=subprocess.STDOUT).returncode
    def launch_pressure(argv, log, run):
        with log.open('a') as stream:
            process = subprocess.Popen(['bash',str(ROOT/'scripts/clawbox'),'--sudo',*argv],
                cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
            requested_stop = False
            while process.poll() is None:
                try:
                    process.wait(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if requested_stop or not (run/'run-state.json').exists():
                    continue
                if not list(run.glob('attempts/*/*/events/*.jsonl')):
                    continue
                snapshot = analyze(run)
                save(out/(run.name+'.live-throughput.json'), snapshot)
                if stable_stop_allowed(snapshot):
                    save(out/(run.name+'.stable-stop.json'), snapshot)
                    rc = cli(['--output-root',str(out/'results'),'experiment','abort',run.name],
                             out/(run.name+'.stop.log'))
                    if rc:
                        raise RuntimeError(f'{run.name}: stable-stop failed; inspect stop log')
                    requested_stop = True
            return process.returncode
    def idle():
        return 'populated 0' in Path('/sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events').read_text().splitlines()
    summaries = [ref]
    full640 = full64 = shared_ref = None
    for row in plan['order']:
        kind = row['kind']; run = out/'results'/row['run_id']
        if run.exists():
            from clawbox.experiments.supervisor import process_is_alive
            current = read(run/'run-state.json')
            while current['state'] not in TERMINAL:
                if not process_is_alive(current.get('supervisor')):
                    raise RuntimeError(f'{run}: supervisor is absent; inspect interruption, do not relaunch')
                row['state'] = 'waiting_for_existing_run'
                save(plan_path, plan)
                time.sleep(30)
                current = read(run/'run-state.json')
            summary=measured_summary(run,out); summaries.append(summary)
            if kind=='full-640-control':full640=summary
            if kind=='full-c64':full64=summary
            if kind in ('full-2560', 'full-4096'):
                shared_ref=summary
                sizes, reason=capacity_points(shared_ref,host['warm_gib'])
                plan.update(capacity_sizes_gib=sizes,capacity_choice=reason)
            row.update(state='retained',summary=summary); save(plan_path,plan); continue
        if row['state'] not in ('pending',):
            continue
        if plan.get('hold_new_runs_for_control_repair'):
            plan['state'] = 'awaiting_control_repair_verification'
            save(plan_path, plan)
            save(out/'summaries.json', summaries)
            return
        if plan.get('deadline_unix_s') and time.time()+600 >= plan['deadline_unix_s']:
            row.update(state='deferred', reason='Less than ten minutes left in user study time budget')
            save(plan_path, plan)
            continue
        if kind.startswith('pressure640-') and kind != 'pressure640-full':
            control = next((r.get('summary') for r in plan['order'] if r['kind']=='pressure640-full'), None)
            if control is None or not control['event_counts'].get('tool_completed'):
                row.update(state='deferred',reason='Pressure640 control has no usable execution data; investigate before ablations')
                save(plan_path,plan); continue
            ready = control['event_counts'].get('task_environment_checked', 0)
            if not ready or control['event_counts'].get('session_failed', 0) > .05*ready:
                row.update(state='deferred',reason='Pressure640 control has excessive session failures; repair before ablations')
                save(plan_path,plan); continue
        if kind == 'full-4096' and shared_ref is not None and shared_ref['event_counts'].get('sandbox_paused', 0):
            row.update(state='deferred', reason='Full-2560 checkpoint triggered; retain its matched ablations.')
            save(plan_path, plan); continue
        if kind in SHARED_KINDS:
            if shared_ref is None:
                row.update(state='deferred',reason='Full-2560 result is unavailable.');save(plan_path,plan);continue
            if row['tasks'] != shared_ref['offered']:
                row.update(state='deferred', reason='Use the shared-pool reference at the selected concurrency.')
                save(plan_path, plan); continue
            pool_used, borrow_used, trigger_reason = shared_pool_trigger(shared_ref)
            if not pool_used:
                row.update(state='deferred',reason=trigger_reason+' Shared pool did not materially trigger.');save(plan_path,plan);continue
            if kind.startswith('no-borrow-') and not borrow_used:
                row.update(state='deferred',reason=trigger_reason+' Borrowing did not trigger; no-borrow would not isolate an active mechanism.');save(plan_path,plan);continue
        if kind.startswith('pool-') and kind in SHARED_KINDS:
            if len(sizes)!=2:
                row.update(state='deferred',reason=reason);save(plan_path,plan);continue
            value=sizes[0 if kind.startswith('pool-low-') else 1]
        else:
            value=None
        if kind.startswith('rate64-'):
            if full64 is None:
                row.update(state='deferred',reason='Full-c64 result is unavailable.');save(plan_path,plan);continue
            if not full64['all_tasks_successful']:
                row.update(state='deferred',reason='Full-c64 did not complete all 64 tasks; population P90 anchor is unavailable.');save(plan_path,plan);continue
            mu, mu_reason = measured_service_rate(full64)
            if not mu:
                row.update(state='deferred',reason=mu_reason);save(plan_path,plan);continue
        if kind=='no-runtime':
            runtime=(full640 or {}).get('mechanisms',{}).get('runtime_memory_control') or {}
            if not runtime.get('memory_protections') and not runtime.get('cpu_boosts'):
                row.update(state='deferred',reason='No memory protection or CPU boost observed; avoid low-value repeat.');save(plan_path,plan);continue
        if kind in ('no-llc','round-robin'):
            if not full640 or not full640['successful_agents'] and not full640['whole_window']['steps_per_s']:
                row.update(state='deferred',reason='No usable matched Full640 control');save(plan_path,plan);continue
            trig=full640['mechanisms']
            active=(trig.get('tool_call_cpu_policy') or {}).get('assignments',0) if kind=='no-llc' else trig.get('template_affinity_decisions',0)
            if not active:
                row.update(state='deferred',reason='Target mechanism did not trigger in matched control');save(plan_path,plan);continue
        spec_base = ledger_base if kind.startswith('ledger-') else base
        if kind.startswith('rate64-'):
            factor={'rate64-light':.5,'rate64-saturation':1.,'rate64-overload':1.5}[kind]
            target=mu*factor
            old=load_experiment(args.base_spec)
            from clawbox.replay.trace import load_trace
            actions=load_trace(Path(old.workload.cases[0].replay_trace_reference))
            duration=max(a.start_s+a.duration_s for a in actions)-min(a.start_s for a in actions)
            duration=max(1.,duration*float(old.inference.configuration.get('time_scale',1)))
            value=63/(target*duration)
            row.update(target_arrival_rate_per_s=target,service_rate_factor=factor,
                       rate_anchor_reason=mu_reason)
        d=make_spec(spec_base,kind,value); h=deepcopy(host)
        if kind.startswith('pressure640-'):
            for node in h['compute_nodes']:
                node.update(memory_gib=224, low_gib=200, high_gib=215)
            if kind == 'pressure640-no-pool':
                h.update(warm=False, shared_borrow_percent=0)
        if kind.startswith('no-pool-') or kind.startswith('ledger-'):
            h.update(warm=False,shared_borrow_percent=0)
        if kind.startswith('pool-') and kind in SHARED_KINDS:h['warm_gib']=value
        if kind in ('full-2560', 'full-4096') or kind in SHARED_KINDS:
            # TAPs cover simultaneously resident VMs, not all offered sessions.
            # The existing 2560 pool is already over 2x Full-1280's measured peak.
            h['cubelet_tap_init_num']=max(2560,h.get('cubelet_tap_init_num',0))
            h['cube_scheduler_overcommit']={'cubebox':{'cpu_ratio':20.,'mem_ratio':6.}}
            h['cube_scheduler_vm_count_ratio']={'cubebox':1.05}
            row['node_vm_limit_plan']='5035 reported VMs * 1.05 = 5287 effective slots for at most 5120 offered VMs.'
            if row['tasks'] == 4096:
                from clawbox.experiments.qualification import host_profile_for
                from clawbox.experiments.cube_capacity import node_capacity
                source_spec = load_experiment(args.base_spec)
                profile = host_profile_for(source_spec)
                node = node_capacity(profile['node'])
                factor = row['tasks'] / max(source_spec.execution.concurrency_levels)
                demand = profile['cube_capacity_demand']
                ratios = {
                    'cpu_ratio': math.ceil(1.05*(demand['cpu_milli']*factor + node.get('QuotaCpuUsage',0))/node['QuotaCpu']),
                    'mem_ratio': math.ceil(1.05*(demand['memory_mib']*factor + node.get('QuotaMemUsage',0))/node['QuotaMem']),
                }
                vm_ratio = math.ceil(100*1.05*(demand['vm_count']*factor + node.get('mvm_num',0))/node['MaxMvmLimit'])/100
                h['cube_scheduler_overcommit']={node['InstanceType']:ratios}
                h['cube_scheduler_vm_count_ratio']={node['InstanceType']:vm_ratio}
                row['node_vm_limit_plan']={
                    'reported_quota_cpu':node['QuotaCpu'], 'reported_quota_mem':node['QuotaMem'],
                    'reported_vm_limit':node['MaxMvmLimit'], 'configured_ratios':ratios,
                    'configured_vm_ratio':vm_ratio,
                    'note':'Configured VM quotas only; cpuset, physical memory limits and memory admission unchanged.'}
        d['experiment_id']=row['run_id'];d['measurement']['output']['directory']=str(out/'results')
        artifact_label = row.get('artifact_label', kind)
        specpath=out/(artifact_label+'.yaml'); hostpath=out/(artifact_label+'.host.yaml')
        specpath.write_text(yaml.safe_dump(d,sort_keys=False));hostpath.write_text(yaml.safe_dump(h,sort_keys=False))
        spec=load_experiment(specpath)
        if kind.startswith('rate64-'):
            last=session_offer_offsets(expand_matrix(spec)[0])[-1]
            d['execution']['arm_timeout_seconds']=math.ceil(
                last+max(1800,2*(full64['completed_only_p90_s'] or full64['measurement_seconds'])))
            specpath.write_text(yaml.safe_dump(d,sort_keys=False));load_experiment(specpath)
        row.update(spec=str(specpath),host=str(hostpath),state='prepared');save(plan_path,plan)
        if not idle():
            plan['state']='blocked_cleanup';save(plan_path,plan)
            raise RuntimeError('VM cgroup is populated; inspect and clean only owned run IDs before continuing')
        rc=cli(['experiment','host','apply',str(hostpath)],out/(artifact_label+'.host.log'))
        if rc:
            row.update(state='host_check_failed',exit_code=rc);save(plan_path,plan);continue
        row.update(state='launching',start_unix_s=time.time());save(plan_path,plan)
        launch_args=['--output-root',str(out/'results'),'experiment','launch',str(specpath),
                     '--run-id',row['run_id'],'--qualification-concurrency','2']
        rc=(launch_pressure(launch_args,out/(artifact_label+'.launch.log'),run)
            if kind.startswith('pressure640-') else cli(launch_args,out/(artifact_label+'.launch.log')))
        row.update(exit_code=rc,end_unix_s=time.time())
        if run.exists() and (run/'run-state.json').exists():
            summary=measured_summary(run,out);row.update(state='measured',summary=summary);summaries.append(summary)
            if kind=='full-640-control':full640=summary
            if kind=='full-c64':full64=summary
            if kind in ('full-2560', 'full-4096'):
                shared_ref=summary
                sizes, reason=capacity_points(shared_ref,host['warm_gib'])
                plan.update(capacity_sizes_gib=sizes,capacity_choice=reason)
        else:
            row.update(state='launch_failed_no_run')
        save(plan_path,plan);save(out/'summaries.json',summaries)
        print(json.dumps({'kind':kind,'state':row['state'],'exit_code':rc}),flush=True)
    plan['state']='queue_finished';save(plan_path,plan);save(out/'summaries.json',summaries)
    if any(r['state'] in ('host_check_failed','launch_failed_no_run','deferred') or r.get('exit_code',0) for r in plan['order']):
        raise SystemExit(2)


if __name__=='__main__':
    main()
