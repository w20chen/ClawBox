#!/usr/bin/env python3
"""Split nested CubeSandbox checkpoint timings into non-overlapping phases.

Reads the dedicated backend JSONL log, not the native ClawTune trace. An optional
ownership file selects one experiment and labels Tool and Runtime sandboxes.
Incomplete or ambiguous checkpoints are reported, never filled with zeros.
"""
import argparse
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath


def records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def contains(parent, child):
    start = parent['started_unix_s']
    end = start + parent['duration_seconds']
    return (start - 0.0001 <= child['started_unix_s'] and
            child['started_unix_s'] + child['duration_seconds'] <= end + 0.0001)


def stats(values):
    ordered = sorted(values)
    def quantile(p):
        i = (len(ordered) - 1) * p
        lo = int(i)
        hi = min(lo + 1, len(ordered) - 1)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (i - lo)
    return dict(count=len(values), total=sum(values), mean=statistics.mean(values),
                p50=quantile(.5), p95=quantile(.95), maximum=max(values))


def analyze(rows, owners=None):
    cube = defaultdict(list)
    vmm = [r for r in rows if r['component'] == 'vmm']
    for row in rows:
        if row['component'] == 'cubelet' and (owners is None or row['sandbox_id'] in owners):
            cube[row['sandbox_id'], row['request_id']].append(row)
    complete, incomplete = [], []
    for (sandbox, request), outer in cube.items():
        try:
            def one(items, phase):
                found = [r for r in items if r['phase'] == phase]
                if len(found) != 1:
                    raise ValueError(f'{phase}: expected one record, found {len(found)}')
                if found[0]['status'] != 'ok':
                    raise ValueError(f'{phase}: operation failed')
                if not math.isfinite(found[0]['duration_seconds']) or found[0]['duration_seconds'] < 0:
                    raise ValueError(f'{phase}: invalid duration')
                return found[0]
            call = one(outer, 'cubelet.vm_snapshot_call')
            snapshot_id = call['snapshot_id']
            snapshots = [r for r in vmm if r['phase'] == 'vm.snapshot_total'
                         and snapshot_id in PurePosixPath(r['target']).parts and contains(call, r)]
            snapshot = one(snapshots, 'vm.snapshot_total')
            inner = [r for r in vmm if r['pid'] == snapshot['pid']
                     and call['started_unix_s'] - 0.0001 <= r['started_unix_s']
                     <= call['started_unix_s'] + call['duration_seconds'] + 0.0001]
            if any(r['status'] != 'ok' for r in inner):
                raise ValueError('failed VMM phase')
            if any(not math.isfinite(r['duration_seconds']) or r['duration_seconds'] < 0 for r in inner):
                raise ValueError('invalid VMM duration')
            if any(not contains(call, r) for r in inner):
                raise ValueError('VMM phase extends beyond snapshot call')
            def duration(phase):
                return one(inner, phase)['duration_seconds']
            def outer_duration(phase):
                return one(outer, phase)['duration_seconds']
            writes = [r for r in inner if r['phase'] == 'memory.write_range']
            if not writes:
                raise ValueError('missing memory.write_range')
            syncs = [r for r in inner if r['phase'] == 'memory.sync_all']
            drops = [r for r in inner if r['phase'] == 'memory.drop_cache']
            if len(syncs) != len(drops):
                raise ValueError('incomplete storage synchronization phases')
            parts = {p: sum(r['duration_seconds'] for r in inner if r['phase'] == p)
                     for p in ('memory.write_range', 'memory.sync_all', 'memory.drop_cache')}
            parts['memory.other'] = duration('memory.send_total') - sum(parts.values())
            parts['vm.freeze'] = duration('vm.freeze')
            parts['vm.capture_state'] = duration('vm.capture_state')
            parts['vm.snapshot_metadata'] = (duration('vm.snapshot_total') -
                                            duration('vm.capture_state') - duration('memory.send_total'))
            parts['vm.delete'] = duration('vm.delete')
            parts['cubelet.shim_call_other'] = (call['duration_seconds'] - duration('vm.freeze') -
                                               duration('vm.snapshot_total') - duration('vm.delete'))
            for phase in ('cubelet.prepare', 'cubelet.rootfs_and_metadata',
                          'cubelet.destroy_runtime', 'cubelet.cleanup_previous'):
                parts[phase] = outer_duration(phase)
            total = outer_duration('cubelet.total')
            parts['cubelet.other'] = total - sum(parts.values())
            if min(parts.values()) < -0.001:
                raise ValueError('nested timings are inconsistent')
            complete.append(dict(sandbox_id=sandbox, request_id=request, snapshot_id=snapshot_id,
                role=(owners or {}).get(sandbox, 'unknown'), total_seconds=total,
                bytes_written=sum(r['bytes_requested'] for r in writes),
                memory_targets=sorted(set(r['target'] for r in writes)),
                exclusive_seconds=parts, inclusive_memory_send_seconds=duration('memory.send_total')))
        except ValueError as error:
            incomplete.append(dict(sandbox_id=sandbox, request_id=request, reason=str(error)))
    summaries = {}
    for role in sorted({r['role'] for r in complete} | {'all'}):
        selected = [r for r in complete if role == 'all' or r['role'] == role]
        if not selected:
            continue
        summaries[role] = dict(checkpoints=len(selected),
            total_seconds=stats([r['total_seconds'] for r in selected]),
            bytes_written=stats([r['bytes_written'] for r in selected]),
            exclusive_seconds={p: stats([r['exclusive_seconds'][p] for r in selected])
                               for p in selected[0]['exclusive_seconds']})
    return dict(schema_version=1, phase_record_counts=dict(Counter(r['phase'] for r in rows)),
                summaries=summaries, complete=complete, incomplete=incomplete)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--ownership', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    owners = None
    if args.ownership:
        owners = {r['sandbox_id']: r['ownership']['session_id'].rsplit('-', 1)[-1]
                  for r in records(args.ownership)}
    result = analyze(records(args.log), owners)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(f"Complete checkpoints: {len(result['complete'])}; incomplete: {len(result['incomplete'])}")
    for role, summary in result['summaries'].items():
        mean = summary['total_seconds']['mean']
        print(f'\n{role}: n={summary["checkpoints"]}, mean total={mean:.6f} s')
        for phase, values in summary['exclusive_seconds'].items():
            percent = 100 * values['mean'] / mean if mean else 0.0
            print(f'  {phase:32s} {values["mean"]:10.6f} s  {percent:6.2f}%')
    if not result['complete'] or result['incomplete']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
