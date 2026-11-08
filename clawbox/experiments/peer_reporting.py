"""Measured ordinary-workflow peer results, with explicit censoring and scope."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import statistics

PHASE_DEFINITIONS = {
    'creation': 'Lifecycle create service, excluding placement and admission queue waits.',
    'checkpoint': 'Lifecycle checkpoint service, including configured compression; one role per sample.',
    'native_restore': 'Full lifecycle restore service: cgroup preparation, decompression, native resume API, placement verification/pinning and restore observation; excludes admission and peer staging/import.',
    'native_restore_api': 'Native resume API call only, nested within lifecycle restore.',
    'decompress': 'snapshot_decompress_s in lifecycle restore; nested within lifecycle restore. Import may separately decompress and includes that in import.',
    'resume_to_ready': 'Worker resume request through lifecycle return, including selection/export/stage/import and CPU/memory admission where applicable; excludes relay reconnect.',
    'runtime_resume_to_runnable': 'Runtime resume request through relay reconnect and preserved agent PID verification.',
    'arrival_to_runnable': 'Frozen offered arrival through initial agent_runnable event, including dispatch/preparation/admission.',
    'export': 'Paused native catalog/dependency discovery and immutable object metadata/hash export.',
    'transfer_stage': 'Per-role stage after manifest registration through ready package publication; includes object queues, TCP service and package construction.',
    'transfer_object': 'Completed object TCP connection/handshake, receive/hash/write and final publication; excludes object lock queue.',
    'transfer_queue': 'Per-object lock queue before its transfer starts.',
    'import': 'Lifecycle bind_replica call: native catalog import plus path reconstruction/rebinding and any import-time decompression.',
    'import_helper': 'Enclosing import_replica elapsed time; nested within import, not additive to its subphases.',
    'import_catalog_lookup': 'Exclusive import-side destination catalog lookup.',
    'import_metadata_alias_rewrite': 'Exclusive import-side metadata and alias rewriting, excluding measured decompression.',
    'import_shared_decompress': 'Exclusive decompression of shared WARM backing during import, before lifecycle restore.',
    'import_target_decompress': 'Exclusive decompression of target-domain backing during import, before lifecycle restore.',
    'import_side_decompress': 'Sum of import shared and target decompression subphases; nested within import_helper/import, never added to those totals.',
    'import_native_api': 'Exclusive native import API call, nested within import_helper/import.',
    'cpu_wait': 'Elapsed full-pair CPU lease reacquisition call; uncontended calls can be nonzero.',
    'memory_wait': 'PolicyCoordinator restore admission elapsed time before lifecycle restore.',
    'reconnect': 'Runtime model relay checkpoint HTTP command after lifecycle restore.',
    'tool_bridge_ready': 'Tool telemetry bridge recovery after lifecycle restore; excludes lifecycle restore itself.',
    'tool_execution': 'Reported execution_started_at through execution_completed_at for completed agent-tool operations.',
}


def _stamp(row):
    if row.get('monotonic_time_ns') is not None:
        return int(row['monotonic_time_ns']) / 1e9
    return row.get('monotonic_s')


def _events(path):
    if path is None or not path.is_file():
        return
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # An interrupted writer can leave a partial final line.
            if isinstance(row, dict):
                yield row


def _dist(values):
    values = sorted(values)
    def quantile(q):
        if not values:
            return None
        index = (len(values)-1)*q
        lower = int(index)
        return values[lower] + (values[min(lower+1, len(values)-1)]-values[lower])*(index-lower)
    return {'n': len(values), 'mean': statistics.fmean(values) if values else None,
            'p50': quantile(.5), 'p90': quantile(.9)}


def is_peer(result):
    storage = ((result.get('arm') or {}).get('resources') or {}).get('snapshot_scheduling_storage') or {}
    return storage.get('topology') == 'peer-local-ssd'


def summarize_peer(result, path):
    arm = result.get('arm') or {}
    arm_id = str(arm.get('arm_id') or '')
    window = closed = None
    for event in _events(path):
        if event.get('event') == 'measurement_window_open':
            window = event
        elif event.get('event') == 'measurement_window_closed':
            closed = event
    origin = window.get('origin_monotonic_s') if window else None
    cutoff = window.get('cutoff_monotonic_s') if window else None
    horizon = window.get('measurement_window_seconds') if window else None
    observed_end = closed.get('observed_end_monotonic_s') if closed else None
    actual_end = min(cutoff, observed_end) if cutoff is not None and observed_end is not None else cutoff
    schedule = window.get('offered_schedule', []) if window else []
    if not schedule:
        schedule = [{'session_id': f'{arm_id}-{index:04d}'} for index in range(arm.get('concurrency', 0))]
    sessions = {row['session_id']: {'arm_id': arm_id, **row, 'status': 'unfinished',
                'dispatched': False, 'agent_runnable': False} for row in schedule}
    for row in sessions.values():
        arrival = row.get('scheduled_monotonic_s')
        row['offered_by_cutoff'] = arrival <= cutoff if arrival is not None and cutoff is not None else None
        if row['offered_by_cutoff'] is False:
            row['status'] = 'not_offered'
    counts = Counter()
    samples = defaultdict(list)
    phases, decisions = [], []
    domains, snapshots = {}, {}
    cpu_samples, node_cpu_samples = [], defaultdict(list)
    transfer_intervals = defaultdict(list)
    memory_peaks, disk_peaks, node_peaks = {}, {}, {}
    concurrency_samples = defaultdict(list)
    backing_observations, created_roles = {}, set()
    node_lost_tails = {}
    payload = protocol = 0
    tools = 0
    source_rows = 0

    def phase(row, name, duration, **values):
        if duration is None or duration < 0:
            return
        samples[name].append(float(duration))
        reference = snapshots.get((row.get('source_domain'), row.get('snapshot_id')), {})
        sid = row.get('session_id') or reference.get('session_id')
        phases.append({'arm_id': arm_id, 'phase': name,
            'session_id': sid, 'role': row.get('role') or reference.get('role'),
            'epoch': row.get('epoch', reference.get('epoch')),
            'transaction_id': row.get('transaction_id') or reference.get('transaction_id'),
            'snapshot_id': row.get('snapshot_id'),
            'source_domain': row.get('source_domain') or domains.get(sid),
            'target_domain': row.get('target_domain') or row.get('compute_domain'),
            'monotonic_s': _stamp(row), 'seconds': float(duration), **values})

    def peak(target, key, value):
        if value is not None:
            target[key] = max(target.get(key, value), value)

    for row in _events(path):
        source_rows += 1
        name, stamp = row.get('event'), _stamp(row)
        if stamp is None or (origin is not None and stamp < origin) or (actual_end is not None and stamp > actual_end):
            continue
        sid = row.get('session_id')
        session = sessions.get(sid)
        if name == 'session_placement':
            domains[sid] = row.get('compute_domain')
            if session is not None:
                session['initial_domain'] = domains[sid]
        elif name == 'session_dispatched' and session is not None:
            session.update(dispatched=True, dispatch_monotonic_s=stamp)
        elif name == 'agent_runnable' and session is not None:
            session.update(agent_runnable=True, runnable_monotonic_s=stamp)
            arrival = session.get('scheduled_monotonic_s')
            if arrival is not None:
                phase(row, 'arrival_to_runnable', stamp-arrival)
        elif name in {'session_complete', 'session_failed'} and session is not None:
            session.update(status=('completed' if name == 'session_complete' and row.get('valid') is True
                                   else 'failed'), terminal_monotonic_s=stamp,
                           terminal_event=name, error_type=row.get('error_type'))
            arrival = session.get('scheduled_monotonic_s')
            if arrival is not None:
                session['arrival_to_terminal_seconds'] = stamp-arrival
        elif name == 'resume_placement_decision':
            counts['decisions'] += 1
            selected = row.get('selected_domain')
            counts['move_decisions' if selected != row.get('source_domain') else 'home_decisions'] += 1
            decisions.append({'arm_id': arm_id, 'session_id': sid, 'epoch': row.get('epoch'),
                'transaction_id': row.get('transaction_id'), 'monotonic_s': stamp,
                'source_domain': row.get('source_domain'), 'selected_domain': selected,
                'policy': row.get('policy'),
                'predicted_ready_seconds_json': json.dumps(row.get('predicted_ready_seconds'), sort_keys=True),
                'candidate_evidence_json': json.dumps(row.get('candidates'), sort_keys=True)})
        elif name == 'session_migration_committed':
            counts['migrations'] += 1
            domains[sid] = row.get('target_domain')
            if session is not None:
                session['migrations'] = session.get('migrations', 0) + 1
        elif name == 'session_migration_failed':
            counts['migration_failures'] += 1
            counts['unknown_migrations'] += not row.get('source_authoritative', False)
        elif name == 'resume_migration_rejected':
            counts['migration_rejections'] += 1
        elif name == 'peer_replica_exported':
            snapshots[row.get('source_domain'), row.get('snapshot_id')] = row
            phase(row, 'export', row.get('export_seconds'))
        elif name == 'peer_replica_ready':
            counts['local_replica_hits'] += bool(row.get('local_replica_hit'))
        elif name == 'peer_native_import_completed':
            phase(row, 'import', row.get('import_seconds'))
            phase(row, 'import_helper', row.get('import_elapsed_seconds'))
            import_phases = row.get('import_phase_seconds') or {}
            for source, target in (
                    ('catalog_lookup_s', 'import_catalog_lookup'),
                    ('metadata_alias_rewrite_s', 'import_metadata_alias_rewrite'),
                    ('shared_decompress_s', 'import_shared_decompress'),
                    ('target_decompress_s', 'import_target_decompress'),
                    ('native_import_s', 'import_native_api')):
                phase(row, target, import_phases.get(source))
            shared, target = import_phases.get('shared_decompress_s'), import_phases.get('target_decompress_s')
            if shared is not None and target is not None:
                phase(row, 'import_side_decompress', shared+target)
        elif name == 'peer_transfer_start':
            counts['transfer_started'] += 1
        elif name == 'peer_transfer_end':
            counts['transfer_completed'] += 1
            payload += row.get('payload_bytes', 0)
            protocol += row.get('protocol_bytes', 0)
            counts['shared_warm_hits'] += row.get('shared_warm_hits') or 0
            counts['object_cache_hits'] += row.get('cache_hits') or 0
            phase(row, 'transfer_stage', row.get('stage_seconds'),
                  payload_bytes=row.get('payload_bytes'), protocol_bytes=row.get('protocol_bytes'),
                  logical_bytes=row.get('logical_bytes'))
            for obj in row.get('objects', []):
                if not obj.get('cache_hit'):
                    phase(row, 'transfer_object', obj.get('transfer_seconds'), payload_bytes=obj.get('payload_bytes'),
                          started_monotonic_s=obj.get('transfer_start_monotonic_s'),
                          ended_monotonic_s=obj.get('transfer_end_monotonic_s'))
                    phase(row, 'transfer_queue', obj.get('queue_seconds'))
                    began, ended = obj.get('payload_start_monotonic_s'), obj.get('payload_end_monotonic_s')
                    if began is not None and ended is not None and ended > began:
                        direction = f"{row.get('source_domain')}->{row.get('target_domain')}"
                        transfer_intervals[direction].append((began, ended, obj['payload_bytes']))
        elif name == 'sandbox_created':
            created_roles.add((sid, row.get('role')))
            if session is not None:
                session['observed_creations'] = session.get('observed_creations', 0) + 1
            phase(row, 'creation', row.get('service_seconds'))
        elif name == 'created_template_backing':
            backing_observations[sid, row.get('role')] = row
            if session is not None:
                role = row.get('role')
                session[f'{role}_warm_base_hit'] = (row.get('warm_base') or {}).get('cache_hit')
                session[f'{role}_local_ssd_template_hit'] = (row.get('local_ssd_base') or {}).get('local_ssd_template_hit')
                session.setdefault('case_id', row.get('case_id'))
                session.setdefault('group_index', row.get('group_index'))
                session.setdefault('rollout_index', row.get('rollout_index'))
        elif name == 'sandbox_paused':
            phase(row, 'checkpoint', row.get('service_seconds'))
        elif name == 'sandbox_restored':
            phase(row, 'native_restore', row.get('service_seconds'))
            phase(row, 'memory_wait', row.get('materialization_reservation_wait_seconds'))
            phase(row, 'resume_to_ready', row.get('resume_to_ready_seconds'))
            stages = (row.get('lifecycle_timing') or {}).get('stage_seconds') or {}
            phase(row, 'decompress', stages.get('snapshot_decompress_s'))
            phase(row, 'native_restore_api', stages.get('api_restore_s'))
        elif name == 'session_cpu_lease_acquired':
            phase(row, 'cpu_wait', row.get('wait_seconds'))
        elif name == 'model_relay_reconnected':
            phase(row, 'reconnect', row.get('service_seconds'))
        elif name == 'session_runtime_resume_ready':
            phase(row, 'runtime_resume_to_runnable', row.get('service_seconds'))
        elif name == 'tool_restore_ready':
            phase(row, 'tool_bridge_ready', row.get('service_seconds'))
        elif name == 'tool_completed' and row.get('execution_scope', 'agent-tool') == 'agent-tool':
            tools += 1
            started, ended = row.get('execution_started_at'), row.get('execution_completed_at')
            phase(row, 'tool_execution', ended-started if ended is not None and started is not None else None)
        if name == 'memory_sample':
            for field in ('warm_allocated_bytes', 'warm_committed_bytes', 'warm_reserved_bytes',
                          'shared_live_used_bytes', 'shared_borrow_reserved_bytes', 'warm_external_allocated_bytes'):
                peak(memory_peaks, field, row.get(field))
            cpu = row.get('cgroup_cpu') or {}
            if cpu.get('usage_usec') is not None:
                cpu_samples.append((stamp, cpu['usage_usec']/1e6))
            for node, item in (row.get('compute_pressure') or {}).items():
                peak(node_peaks, node, item.get('resident_bytes'))
            pressure = row.get('compute_pressure') or {}
            for field in ('running_vms', 'resident_pairs', 'resident_sessions'):
                if pressure and all(item.get(field) is not None for item in pressure.values()):
                    concurrency_samples[field].append((stamp, sum(item[field] for item in pressure.values())))
            for node, item in (row.get('peer_storage_sample') or {}).items():
                for field in ('filesystem_used_bytes', 'reserved_transfer_payload_bytes'):
                    peak(disk_peaks.setdefault(node, {}), field, item.get(field))
        peer_cpu = row.get('peer_cpu') if name == 'memory_sample' else row if name == 'peer_cpu_sample' else None
        if peer_cpu:
            for node, item in (peer_cpu.get('nodes') or {}).items():
                if item.get('observed_cpu_seconds') is not None:
                    node_cpu_samples[node].append((stamp, item['observed_cpu_seconds']))
                peak(node_lost_tails, node, item.get('unobserved_exit_tails'))
    for sid, row in sessions.items():
        row['final_domain'] = domains.get(sid)
    statuses = Counter(row['status'] for row in sessions.values())
    grouped = defaultdict(list)
    for session in sessions.values():
        if session.get('case_id') is not None:
            grouped[session['case_id']].append(session)
    groups = []
    for case_id, members in grouped.items():
        completed = [row for row in members if row['status'] == 'completed']
        values = [row['arrival_to_terminal_seconds'] for row in completed
                  if row.get('arrival_to_terminal_seconds') is not None]
        observed_warm = [row[role+'_warm_base_hit'] for row in members for role in ('runtime', 'tool')
                         if row.get(role+'_warm_base_hit') is not None]
        groups.append({'arm_id': arm_id, 'case_id': case_id,
            'group_index': members[0].get('group_index'), 'planned_rollouts': len(members),
            'completed_rollouts': len(completed),
            'failed_rollouts': sum(row['status'] == 'failed' for row in members),
            'unfinished_rollouts': sum(row['status'] == 'unfinished' for row in members),
            'completion_fraction': len(completed)/len(members),
            'completed_latency_p50_seconds': _dist(values)['p50'],
            'completed_latency_p90_seconds': _dist(values)['p90'],
            'observed_creations': sum(row.get('observed_creations', 0) for row in members),
            'warm_base_hits': sum(value is True for value in observed_warm) if observed_warm else None,
            'warm_base_misses': sum(value is False for value in observed_warm) if observed_warm else None})
    has_events = path is not None and path.is_file() and source_rows > 0
    valid_window = bool(window and closed and (closed.get('cutoff_reached')
                        or closed.get('reason') == 'all_sessions_terminal'))
    def cpu_result(points):
        points = sorted(points)
        if len(points) < 2 or points[-1][0] <= points[0][0]:
            return {'cpu_seconds': None, 'sample_interval_seconds': None, 'mean_cores': None}
        seconds = points[-1][1]-points[0][1]
        interval = points[-1][0]-points[0][0]
        return {'cpu_seconds': seconds if seconds >= 0 else None,
                'sample_interval_seconds': interval, 'mean_cores': seconds/interval if seconds >= 0 else None}
    def concurrency_result(points):
        points = sorted(points)
        span = points[-1][0]-points[0][0] if len(points) > 1 else None
        return {'sampled_peak': max((value for _, value in points), default=None),
            'sample_interval_seconds': span,
            'time_weighted_sample_mean': (sum(value*(end-start) for (start, value), (end, _) in zip(points, points[1:]))/span
                                          if span is not None and span > 0 else None),
            'scope': 'observed sampled occupancy; left-sample hold between observations, not CPU execution concurrency'}
    nodes = ((arm.get('resources') or {}).get('snapshot_scheduling_storage') or {}).get('local_roots', {})
    links = {}
    for direction, intervals in transfer_intervals.items():
        ordered = sorted(intervals)
        beginning, ending, _ = ordered[0]
        busy = 0.
        for began, ended, _ in ordered[1:]:
            if began > ending:
                busy += ending-beginning
                beginning, ending = began, ended
            else:
                ending = max(ending, ended)
        busy += ending-beginning
        active = maximum = 0
        for _, change in sorted([(began, 1) for began, _, _ in intervals]
                                + [(ended, -1) for _, ended, _ in intervals]):
            active += change
            maximum = max(maximum, active)
        transferred = sum(size for _, _, size in intervals)
        links[direction] = {'completed_object_payload_bytes': transferred,
            'payload_active_union_seconds': busy, 'peak_overlapping_payloads': maximum,
            'effective_payload_bytes_per_second': transferred/busy,
            'measurement_scope': 'TCP receive, checksum and destination writes; completed objects only'}
    summary = {'arm_id': arm_id, 'policy': (arm.get('execution') or {}).get('resume_policy', 'home'),
        'status': result.get('status'), 'events_available': has_events,
        'base_groups': groups, 'planned_base_count': len(groups) if groups else None,
        'observed_created_vm_count': len(created_roles) if has_events else None,
        'observed_warm_base_hits': sum((row.get('warm_base') or {}).get('cache_hit') is True for row in backing_observations.values()) if backing_observations else None,
        'observed_warm_base_misses': sum((row.get('warm_base') or {}).get('cache_hit') is False for row in backing_observations.values()) if backing_observations else None,
        'observed_local_ssd_template_hits': sum((row.get('local_ssd_base') or {}).get('local_ssd_template_hit') is True for row in backing_observations.values()) if backing_observations else None,
        'actual_resident_concurrency': {field: concurrency_result(concurrency_samples[field])
                                      for field in ('running_vms', 'resident_pairs', 'resident_sessions')},
        'measurement_window_seconds': horizon, 'fixed_window_observed': valid_window,
        'actual_observed_seconds': closed.get('actual_observed_seconds') if closed else None,
        'planned_sessions': len(sessions), 'offered_by_cutoff': sum(row['offered_by_cutoff'] is True for row in sessions.values()) if window else None,
        'dispatched_sessions': sum(row['dispatched'] for row in sessions.values()) if has_events else None,
        'completed_sessions': statuses['completed'] if has_events else None,
        'failed_sessions': statuses['failed'] if has_events else None,
        'unfinished_sessions': statuses['unfinished'], 'not_offered_sessions': statuses['not_offered'],
        'tool_completions': tools if has_events else None,
        'tool_completion_unit': 'completed agent-tool SSH bridge operations, including exec and filesystem operations; not LLM tool calls; excludes backend-maintenance',
        'traces_per_second_fixed_window': statuses['completed']/horizon if valid_window and horizon else None,
        'tools_per_second_fixed_window': tools/horizon if valid_window and horizon else None,
        **{key: counts[key] if has_events else None for key in ('decisions', 'move_decisions', 'home_decisions',
            'migrations', 'migration_failures', 'migration_rejections', 'unknown_migrations',
            'local_replica_hits', 'object_cache_hits', 'shared_warm_hits')},
        'incomplete_transfers': max(0, counts['transfer_started']-counts['transfer_completed']) if has_events else None,
        'completed_transfer_payload_bytes': payload if has_events else None,
        'transfer_payload_bytes': payload if has_events and counts['transfer_started'] == counts['transfer_completed'] else None,
        'transfer_protocol_bytes': protocol if has_events and counts['transfer_started'] == counts['transfer_completed'] else None,
        'transfer_interface_bytes': None,
        'transfer_directions': links,
        'distributions_seconds': {name: _dist(samples[name]) for name in (
            'creation', 'checkpoint', 'native_restore', 'native_restore_api', 'resume_to_ready', 'arrival_to_runnable',
            'export', 'transfer_stage', 'transfer_object', 'transfer_queue', 'import', 'decompress',
            'import_helper', 'import_catalog_lookup', 'import_metadata_alias_rewrite',
            'import_shared_decompress', 'import_target_decompress', 'import_side_decompress', 'import_native_api',
            'cpu_wait', 'memory_wait', 'reconnect', 'runtime_resume_to_runnable',
            'tool_bridge_ready', 'tool_execution')},
        'phase_definitions': PHASE_DEFINITIONS,
        'cpu': cpu_result(cpu_samples), 'memory_sampled_peaks': memory_peaks,
        'nodes': {node: {'cpu': {**cpu_result(node_cpu_samples[node]), 'coverage': 'sampled_lower_bound',
                                'unobserved_exit_tails': node_lost_tails.get(node)},
                         'resident_sampled_peak_bytes': node_peaks.get(node),
                         'ssd_sampled_peaks': disk_peaks.get(node, {})} for node in nodes},
        'notes': ['Throughput uses the frozen horizon; latency is conditional on an observed terminal event.',
                  'CPU means use observed counter intervals, never the unobserved remainder of an early-finished horizon.',
                  'SSD figures are sampled filesystem allocation peaks; payload bytes are not physical disk I/O.',
                  'Interface bytes are unavailable per transfer; concurrent link counters belong to network qualification.',
                  'Phase durations are nested and can overlap; never sum phase quantiles or durations across concurrent sessions.',
                  'Rollouts of the same base replay are correlated; peer-groups.csv is the matched base-level comparison, not 1280 independent task samples.',
                  'WARM base hits use observed private VM mapping device/inode identity. Local SSD template hits corroborate selected catalog plus actual open rootfs backing, not physical I/O or page-cache hits.',
                  'Only phases completed by the observation cutoff enter latency distributions; unfinished operations are censored.',
                  'Predicted ready times are kept only in the separate decisions CSV.']}
    return summary, list(sessions.values()), phases, decisions


def render_peer_lines(run_root, results, event_path):
    summaries = [summarize_peer(result, event_path(run_root, result))[0]
                 for result in results if is_peer(result)]
    if not summaries:
        return []
    def number(value, digits=3):
        return 'unknown' if value is None else f'{value:.{digits}f}'
    def distribution(row, name):
        values = row['distributions_seconds'][name]
        return f"{number(values['p50'])}/{number(values['p90'])} (n={values['n']})"
    lines = ['## Peer restore: fixed-window outcomes', '',
        'These rates use the frozen offered-arrival horizon, including unfinished sessions. '
        'The general Agents/min table below retains its historical completed-workload denominator and is not this comparison. '
        'A fixed window marked unobserved has no throughput estimate.', '',
        '| Arm | Resume policy | Horizon / observed s | Window observed | Completed / failed / unfinished / planned | Agent-tool SSH ops/s | Traces/s | Move decisions / committed / failed / rejected |',
        '|---|---|---:|---|---:|---:|---:|---:|']
    for row in summaries:
        lines.append(f"| {row['arm_id']} | {row['policy']} | {number(row['measurement_window_seconds'])}/{number(row['actual_observed_seconds'])} | "
            f"{row['fixed_window_observed']} | {row['completed_sessions']}/{row['failed_sessions']}/{row['unfinished_sessions']}/{row['planned_sessions']} | "
            f"{number(row['tools_per_second_fixed_window'])} | {number(row['traces_per_second_fixed_window'])} | "
            f"{row['move_decisions']}/{row['migrations']}/{row['migration_failures']}/{row['migration_rejections']} |")
    lines += ['', 'Agent-tool SSH operations include command execution and filesystem bridge operations. '
              'They exclude backend-maintenance and are not a count of LLM tool calls.', '',
              '| Arm | Original bases | Observed VM creations | Actual WARM base hits / misses | Local SSD template hits | Sampled resident pairs peak / mean | Occupancy sample interval s |',
              '|---|---:|---:|---:|---:|---:|---:|']
    for row in summaries:
        occupancy = row['actual_resident_concurrency']['resident_pairs']
        lines.append(f"| {row['arm_id']} | {row['planned_base_count']} | {row['observed_created_vm_count']} | "
            f"{row['observed_warm_base_hits']}/{row['observed_warm_base_misses']} | {row['observed_local_ssd_template_hits']} | "
            f"{number(occupancy['sampled_peak'])}/{number(occupancy['time_weighted_sample_mean'])} | {number(occupancy['sample_interval_seconds'])} |")
    lines += ['', 'Resident pairs count both roles resident; partially resident sessions and resident VM counts are separate in JSON. '
              'These are sampled occupancy measurements, not offered concurrency or CPU execution counts. '
              'Base-cache hits require actual VM private mappings to prepared device/inode identities; SSD template hits '
              'require the selected local catalog and an observed open local rootfs descriptor. Neither is a physical I/O hit rate.']
    lines += ['', '| Arm | Creation p50/p90 s | Checkpoint p50/p90 s | Lifecycle restore p50/p90 s | Resume-to-native-ready p50/p90 s | Runtime resume-to-runnable p50/p90 s | Arrival-to-runnable p50/p90 s |',
              '|---|---:|---:|---:|---:|---:|---:|']
    for row in summaries:
        lines.append('| ' + row['arm_id'] + ' | ' + ' | '.join(distribution(row, name) for name in
            ('creation', 'checkpoint', 'native_restore', 'resume_to_ready', 'runtime_resume_to_runnable', 'arrival_to_runnable')) + ' |')
    lines += ['', '| Arm | Payload / protocol MiB | Incomplete transfers | Local replica / object cache / shared WARM hits | Transfer stage p50/p90 s | Import p50/p90 s | CPU wait p50/p90 s | Memory wait p50/p90 s | Reconnect p50/p90 s |',
              '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in summaries:
        byte_values = [row['transfer_payload_bytes'], row['transfer_protocol_bytes']]
        lines.append('| ' + row['arm_id'] + ' | ' + '/'.join(number(value/1024**2 if value is not None else None) for value in byte_values)
            + f" | {row['incomplete_transfers']} | {row['local_replica_hits']}/{row['object_cache_hits']}/{row['shared_warm_hits']} | "
            + ' | '.join(distribution(row, name) for name in ('transfer_stage', 'import', 'cpu_wait', 'memory_wait', 'reconnect')) + ' |')
    lines += ['', 'Phase distributions use actual event intervals. They must not be added across overlapping sessions. '
        'Transfer payload/protocol bytes do not measure physical SSD I/O; per-transfer interface bytes are unknown. '
        'An interrupted transfer has an unknown total, with completed payload retained separately in JSON.', '',
        '| Arm | Import WARM decompression p50/p90 s | Import target decompression p50/p90 s | Native import API p50/p90 s | Lifecycle restore decompression p50/p90 s |',
        '|---|---:|---:|---:|---:|']
    for row in summaries:
        lines.append('| ' + row['arm_id'] + ' | ' + ' | '.join(distribution(row, name) for name in
            ('import_shared_decompress', 'import_target_decompress', 'import_native_api', 'decompress')) + ' |')
    lines += ['', 'Import-side subphases are exclusive components of the import helper. Its elapsed time and the outer '
        'bind total enclose these components; do not add either total to the components. Lifecycle decompression '
        'is measured later and remains separate.', '',
        '| Arm | Domain | VM CPU seconds / observed interval s | Mean occupied logical cores | Unobserved CPU exit tails | Sampled resident GiB | Sampled SSD used / transfer-reserved GiB |',
        '|---|---|---:|---:|---:|---:|---:|']
    for row in summaries:
        for node, values in row['nodes'].items():
            cpu, disk = values['cpu'], values['ssd_sampled_peaks']
            bytes_values = [values['resident_sampled_peak_bytes'], disk.get('filesystem_used_bytes'), disk.get('reserved_transfer_payload_bytes')]
            gb = [number(value/1024**3 if value is not None else None) for value in bytes_values]
            lines.append(f"| {row['arm_id']} | {node} | {number(cpu['cpu_seconds'])}/{number(cpu['sample_interval_seconds'])} | "
                f"{number(cpu['mean_cores'])} | {cpu['unobserved_exit_tails']} | {gb[0]} | {gb[1]}/{gb[2]} |")
    lines += ['', '| Arm | Direction | Completed payload MiB | Union active payload s | Peak overlapping payloads | Effective payload Mbit/s |',
              '|---|---|---:|---:|---:|---:|']
    for row in summaries:
        for direction, values in row['transfer_directions'].items():
            lines.append(f"| {row['arm_id']} | {direction} | {number(values['completed_object_payload_bytes']/1024**2)} | "
                f"{number(values['payload_active_union_seconds'])} | {values['peak_overlapping_payloads']} | "
                f"{number(values['effective_payload_bytes_per_second']*8/1e6)} |")
    lines += ['', 'Effective payload rate uses the union of measured TCP receive/checksum/write intervals; '
              'overlapping streams share one time denominator. It is not a configured link limit or an interface-byte measurement.']
    lines += ['', '| Arm | Parent cgroup CPU seconds / observed interval s | Parent mean cores | Sampled WARM allocated / committed GiB | Sampled shared live / borrow reserved GiB |',
              '|---|---:|---:|---:|---:|']
    for row in summaries:
        cpu, memory = row['cpu'], row['memory_sampled_peaks']
        gb = [number(memory[key]/1024**3 if key in memory else None) for key in
              ('warm_allocated_bytes', 'warm_committed_bytes', 'shared_live_used_bytes', 'shared_borrow_reserved_bytes')]
        lines.append(f"| {row['arm_id']} | {number(cpu['cpu_seconds'])}/{number(cpu['sample_interval_seconds'])} | "
            f"{number(cpu['mean_cores'])} | {gb[0]}/{gb[1]} | {gb[2]}/{gb[3]} |")
    lines += ['', 'Per-domain CPU uses verified Runtime/Tool cgroup counters and records migration boundaries. '
        'Its sampled totals are lower bounds when a disappearing cgroup leaves an unobserved exit tail. '
        'CPU averages use the observed interval; no CPU or memory use is extrapolated over an early-finished window. '
        'SSD and memory peaks are sampled lower bounds, not proven instantaneous maxima.', '',
        'Every planned session and its group/rollout identity is retained in `peer-sessions.csv`; '
        '`peer-groups.csv` aggregates correlated rollouts by original base case. Measured phases are in `peer-phases.csv`; '
        'predictions and their provenance are separate in `peer-decisions.csv`. '
        '`peer-summary.json` and `peer-summary.csv` retain counts, sample sizes, missing values and the exact denominator.', '']
    return lines


def write_peer_artifacts(run_root, results, event_path):
    summaries, sessions, phases, decisions = [], [], [], []
    for result in results:
        if not is_peer(result):
            continue
        summary, session_rows, phase_rows, decision_rows = summarize_peer(result, event_path(run_root, result))
        summaries.append(summary); sessions.extend(session_rows); phases.extend(phase_rows); decisions.extend(decision_rows)
    if not summaries:
        return
    (run_root/'peer-summary.json').write_text(json.dumps(summaries, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    flat = []
    for summary in summaries:
        row = {key: value for key, value in summary.items() if not isinstance(value, (dict, list))}
        for phase, values in summary['distributions_seconds'].items():
            row.update({f'{phase}_{stat}': value for stat, value in values.items()})
        flat.append(row)
    for filename, records in (('peer-summary.csv', flat), ('peer-sessions.csv', sessions),
                              ('peer-groups.csv', [group for summary in summaries for group in summary['base_groups']]),
                              ('peer-phases.csv', phases), ('peer-decisions.csv', decisions)):
        keys = list(dict.fromkeys(key for row in records for key in row))
        with (run_root/filename).open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=keys)
            writer.writeheader()
            writer.writerows(records)
