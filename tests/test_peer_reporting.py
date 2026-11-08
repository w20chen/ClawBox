import csv
import json

import pytest

from clawbox.experiments.peer_reporting import summarize_peer, write_peer_artifacts
from clawbox.experiments.reporting import render_run_report


def record(name, at, **values):
    return {'event': name, 'monotonic_time_ns': str(int(at*1e9)), **values}


def fixture(tmp_path):
    result = {'arm': {'arm_id': 'arm', 'concurrency': 3,
        'execution': {'resume_policy': 'cost-aware'}, 'policy': {'name': 'full'},
        'resources': {'snapshot_scheduling_storage': {'topology': 'peer-local-ssd',
                        'local_roots': {'node0': '/node0', 'node1': '/node1'}}}},
        'status': 'timed_out', 'performance': {}, 'correctness': {}, 'memory': {}}
    events = [record('measurement_window_open', 10, origin_monotonic_s=10,
                cutoff_monotonic_s=20, measurement_window_seconds=10,
                offered_schedule=[{'session_id': f'arm-{i:04}', 'scheduled_monotonic_s': 10+i} for i in range(3)]),
        record('session_dispatched', 10, session_id='arm-0000'),
        record('session_placement', 10.5, session_id='arm-0000', compute_domain='node0'),
        record('memory_sample', 11, cgroup_cpu={'usage_usec': 3_000_000},
            peer_cpu={'nodes': {'node0': {'observed_cpu_seconds': 1., 'unobserved_exit_tails': 0}}},
            warm_allocated_bytes=100, shared_borrow_reserved_bytes=50,
            peer_storage_sample={'node0': {'filesystem_used_bytes': 1000, 'reserved_transfer_payload_bytes': 128}},
            compute_pressure={'node0': {'resident_bytes': 500}}),
        record('session_failed', 12, session_id='arm-0001'),
        record('peer_replica_exported', 12, session_id='arm-0000', source_domain='node0',
            snapshot_id='snap', role='runtime', epoch=0, transaction_id='tx', export_seconds=.1),
        record('resume_placement_decision', 12.1, session_id='arm-0000', source_domain='node0',
            selected_domain='node1', policy='cost-aware', predicted_ready_seconds={'node0': 999, 'node1': 888}),
        record('peer_transfer_start', 12.2, source_domain='node0', target_domain='node1', snapshot_id='snap'),
        record('tool_completed', 13, execution_scope='agent-tool', execution_started_at=10, execution_completed_at=12),
        record('peer_transfer_end', 14, source_domain='node0', target_domain='node1', snapshot_id='snap',
            payload_bytes=128, protocol_bytes=32, logical_bytes=1024, cache_hits=2, shared_warm_hits=1,
            stage_seconds=1.8, objects=[{'cache_hit': False, 'payload_bytes': 128, 'transfer_seconds': 1.5, 'queue_seconds': .2,
                                      'payload_start_monotonic_s': 12.3, 'payload_end_monotonic_s': 13.8}]),
        record('peer_native_import_completed', 14.1, session_id='arm-0000', role='runtime', import_seconds=.1,
            import_elapsed_seconds=.09, import_phase_seconds={'catalog_lookup_s': .01,
                'metadata_alias_rewrite_s': .01, 'shared_decompress_s': .02,
                'target_decompress_s': .03, 'native_import_s': .02}),
        record('session_migration_committed', 14.5, session_id='arm-0000', target_domain='node1'),
        record('sandbox_restored', 14.6, session_id='arm-0000', role='runtime', service_seconds=.2,
            resume_to_ready_seconds=2.6, materialization_reservation_wait_seconds=.05,
            lifecycle_timing={'stage_seconds': {'snapshot_decompress_s': .03, 'api_restore_s': .12},
                'stage_spans': [{'stage': 'snapshot_decompress_s', 'start_ns': 14000000000, 'end_ns': 14030000000}]}),
        record('session_complete', 15, session_id='arm-0000', valid=True),
        record('session_dispatched', 16, session_id='arm-0002'),
        record('tool_completed', 17, execution_scope='agent-tool', execution_started_at=15, execution_completed_at=16),
        record('memory_sample', 19, cgroup_cpu={'usage_usec': 7_000_000},
            peer_cpu={'nodes': {'node0': {'observed_cpu_seconds': 3., 'unobserved_exit_tails': 1}}},
            warm_allocated_bytes=200, compute_pressure={'node0': {'resident_bytes': 700}}),
        record('measurement_window_closed', 20.1, observed_end_monotonic_s=20,
            actual_observed_seconds=10, cutoff_reached=True, reason='cutoff'),
        record('session_complete', 21, session_id='arm-0002', valid=True),
        record('memory_sample', 22, warm_allocated_bytes=999999, cgroup_cpu={'usage_usec': 99_000_000})]
    path = tmp_path/'events.jsonl'
    path.write_text(''.join(json.dumps(row)+'\n' for row in events))
    result['artifacts'] = {'events': str(path)}
    return result, path, events


def test_peer_report_keeps_censored_sessions_and_uses_frozen_horizon(tmp_path):
    result, path, _ = fixture(tmp_path)
    summary, sessions, phases, decisions = summarize_peer(result, path)
    assert (summary['completed_sessions'], summary['failed_sessions'], summary['unfinished_sessions']) == (1, 1, 1)
    assert summary['traces_per_second_fixed_window'] == .1
    assert summary['tools_per_second_fixed_window'] == .2
    assert summary['migrations'] == 1 and summary['move_decisions'] == 1
    assert sessions[-1]['status'] == 'unfinished'
    assert summary['distributions_seconds']['native_restore']['p50'] == .2
    assert summary['distributions_seconds']['native_restore_api']['p50'] == .12
    assert summary['distributions_seconds']['decompress']['p50'] == .03
    assert summary['distributions_seconds']['import']['p50'] == .1
    assert summary['distributions_seconds']['import_helper']['p50'] == .09
    assert summary['distributions_seconds']['import_side_decompress']['p50'] == .05
    assert summary['distributions_seconds']['import_native_api']['p50'] == .02
    assert summary['distributions_seconds']['transfer_object']['p50'] == 1.5
    assert '999' in decisions[0]['predicted_ready_seconds_json']
    assert all(row['seconds'] < 10 for row in phases)
    transfer = next(row for row in phases if row['phase'] == 'transfer_stage')
    assert transfer['session_id'] == 'arm-0000' and transfer['role'] == 'runtime'
    assert transfer['transaction_id'] == 'tx'


def test_peer_resource_means_do_not_extrapolate_or_use_post_cutoff_samples(tmp_path):
    result, path, _ = fixture(tmp_path)
    summary, *_ = summarize_peer(result, path)
    assert summary['cpu'] == {'cpu_seconds': 4., 'sample_interval_seconds': 8., 'mean_cores': .5}
    assert summary['nodes']['node0']['cpu']['mean_cores'] == .25
    assert summary['nodes']['node1']['cpu']['mean_cores'] is None
    assert summary['memory_sampled_peaks']['warm_allocated_bytes'] == 200
    assert summary['nodes']['node0']['ssd_sampled_peaks']['filesystem_used_bytes'] == 1000
    assert summary['transfer_payload_bytes'] == 128
    assert summary['transfer_interface_bytes'] is None


def test_unknown_evidence_and_incomplete_transfers_are_not_zero(tmp_path):
    result, path, events = fixture(tmp_path)
    missing, *_ = summarize_peer(result, tmp_path/'absent')
    assert missing['completed_sessions'] is None
    assert missing['traces_per_second_fixed_window'] is None
    assert missing['unfinished_sessions'] == 3
    events = [row for row in events if row['event'] != 'peer_transfer_end']
    path.write_text(''.join(json.dumps(row)+'\n' for row in events))
    incomplete, *_ = summarize_peer(result, path)
    assert incomplete['incomplete_transfers'] == 1
    assert incomplete['transfer_payload_bytes'] is None
    assert incomplete['completed_transfer_payload_bytes'] == 0


def test_peer_csv_and_normal_report_retain_all_planned_sessions(tmp_path):
    result, path, _ = fixture(tmp_path)
    write_peer_artifacts(tmp_path, [result], lambda root, result: path)
    rows = list(csv.DictReader((tmp_path/'peer-sessions.csv').open()))
    assert len(rows) == 3
    assert rows[-1]['status'] == 'unfinished'
    (tmp_path/'arms').mkdir()
    (tmp_path/'arms'/'arm.json').write_text(json.dumps(result))
    report = render_run_report(tmp_path)
    assert 'fixed-window outcomes' in report
    assert '1/1/1/3' in report
    assert 'sampled lower bounds' in report
    assert 'historical completed-workload denominator' in report


def test_early_completion_keeps_full_rate_denominator_without_resource_extrapolation(tmp_path):
    result, path, events = fixture(tmp_path)
    events = [row for row in events if int(row['monotonic_time_ns'])/1e9 <= 15]
    events[0].update(cutoff_monotonic_s=110, measurement_window_seconds=100)
    events.append(record('measurement_window_closed', 15.1, observed_end_monotonic_s=15,
                         actual_observed_seconds=5, cutoff_reached=False, reason='all_sessions_terminal'))
    path.write_text(''.join(json.dumps(row)+'\n' for row in events))
    summary, *_ = summarize_peer(result, path)
    assert summary['traces_per_second_fixed_window'] == .01
    assert summary['actual_observed_seconds'] == 5
    assert summary['cpu']['sample_interval_seconds'] is None  # Only one observed sample.


def test_direction_payload_rate_uses_union_of_overlapping_streams(tmp_path):
    result, path, events = fixture(tmp_path)
    events.extend([
        record('peer_transfer_start', 12.9, source_domain='node0', target_domain='node1', snapshot_id='second'),
        record('peer_transfer_end', 14.1, source_domain='node0', target_domain='node1', snapshot_id='second',
            payload_bytes=64, protocol_bytes=20, objects=[{'cache_hit': False, 'payload_bytes': 64,
                'payload_start_monotonic_s': 13., 'payload_end_monotonic_s': 14.}])])
    path.write_text(''.join(json.dumps(row)+'\n' for row in events))
    summary, *_ = summarize_peer(result, path)
    direction = summary['transfer_directions']['node0->node1']
    assert direction['peak_overlapping_payloads'] == 2
    assert direction['payload_active_union_seconds'] == pytest.approx(1.7)
    assert direction['effective_payload_bytes_per_second'] == pytest.approx(192/1.7)


def test_base_groups_keep_correlated_rollouts_censored_and_measure_real_occupancy(tmp_path):
    result, path, events = fixture(tmp_path)
    for i, offered in enumerate(events[0]['offered_schedule']):
        offered.update(case_id='base-a' if i < 2 else 'base-b', group_index=i//2, rollout_index=i%2)
    events.extend([
        record('created_template_backing', 11.1, session_id='arm-0000', role='runtime',
            warm_base={'cache_hit': True}, local_ssd_base={'local_ssd_template_hit': True}),
        record('created_template_backing', 11.2, session_id='arm-0000', role='tool',
            warm_base={'cache_hit': False}, local_ssd_base={'local_ssd_template_hit': True}),
        record('memory_sample', 12, compute_pressure={'node0': {'resident_pairs': 2, 'resident_sessions': 3, 'running_vms': 5},
                                                    'node1': {'resident_pairs': 1, 'resident_sessions': 1, 'running_vms': 2}}),
        record('memory_sample', 18, compute_pressure={'node0': {'resident_pairs': 1, 'resident_sessions': 1, 'running_vms': 2},
                                                    'node1': {'resident_pairs': 0, 'resident_sessions': 0, 'running_vms': 0}}),
    ])
    path.write_text(''.join(json.dumps(row)+'\n' for row in events))
    summary, sessions, *_ = summarize_peer(result, path)
    assert summary['planned_base_count'] == 2
    first, second = summary['base_groups']
    assert first['completion_fraction'] == .5 and first['failed_rollouts'] == 1
    assert second['unfinished_rollouts'] == 1 and second['completed_latency_p50_seconds'] is None
    assert first['warm_base_hits'] == first['warm_base_misses'] == 1
    assert second['warm_base_hits'] is None
    assert sessions[0]['group_index'] == 0 and sessions[1]['rollout_index'] == 1
    occupancy = summary['actual_resident_concurrency']['resident_pairs']
    assert occupancy['sampled_peak'] == occupancy['time_weighted_sample_mean'] == 3
    assert occupancy['sample_interval_seconds'] == 6
