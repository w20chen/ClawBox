import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    'checkpoint_report', Path(__file__).parents[1] / 'scripts/report-checkpoint-phases.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def sample():
    rows = []
    def add(phase, start, duration):
        rows.append(dict(component='cubelet' if phase.startswith('cubelet.') else 'vmm',
            phase=phase, started_unix_s=100 + start, duration_seconds=duration,
            sandbox_id='box', request_id='request', snapshot_id='snap-1',
            target='/snap-1/memory', pid=42, bytes_requested=4096, status='ok'))
    add('cubelet.total', 0, 10)
    add('cubelet.prepare', 0, 1)
    add('cubelet.vm_snapshot_call', 1, 7)
    add('vm.freeze', 1, .1)
    add('vm.snapshot_total', 1.1, 6)
    add('vm.capture_state', 1.1, .2)
    add('memory.send_total', 1.5, 5)
    add('memory.write_range', 1.5, 2)
    add('memory.write_range', 3.5, 1)
    add('memory.sync_all', 4.5, 1)
    add('memory.drop_cache', 5.5, .2)
    add('vm.delete', 7.1, .5)
    add('cubelet.rootfs_and_metadata', 8, 1)
    add('cubelet.destroy_runtime', 9, .5)
    add('cubelet.cleanup_previous', 9.5, .4)
    return rows


def test_exclusive_parts_do_not_double_count():
    result = report.analyze(sample(), {'box': 'tool'})
    assert not result['incomplete']
    row, = result['complete']
    assert row['bytes_written'] == 8192
    assert row['exclusive_seconds']['memory.write_range'] == 3
    assert abs(sum(row['exclusive_seconds'].values()) - 10) < 1e-9


def test_missing_memory_data_is_not_zero():
    result = report.analyze([r for r in sample() if r['phase'] != 'memory.write_range'])
    assert not result['complete']
    assert 'missing memory.write_range' in result['incomplete'][0]['reason']


def test_tmpfs_can_omit_sync_and_cache_drop():
    result = report.analyze([r for r in sample() if r['phase'] not in
                            {'memory.sync_all', 'memory.drop_cache'}])
    assert not result['incomplete']


def test_ambiguous_snapshot_is_rejected():
    rows = sample()
    rows.append(next(r.copy() for r in rows if r['phase'] == 'vm.snapshot_total'))
    assert report.analyze(rows)['incomplete']


def test_ownership_excludes_other_experiments():
    assert not report.analyze(sample(), {'another-box': 'tool'})['complete']


def test_missing_storage_phase_is_incomplete():
    rows = [r for r in sample() if r['phase'] != 'memory.drop_cache']
    assert not report.analyze(rows)['complete']
    assert 'synchronization' in report.analyze(rows)['incomplete'][0]['reason']


def test_snapshot_id_must_match_a_path_component():
    rows = sample()
    for row in rows:
        if row['component'] == 'vmm':
            row['target'] = '/snap-10/memory'
    assert not report.analyze(rows)['complete']


def test_nonfinite_write_duration_is_rejected():
    rows = sample()
    next(r for r in rows if r['phase'] == 'memory.write_range')['duration_seconds'] = float('nan')
    assert not report.analyze(rows)['complete']
