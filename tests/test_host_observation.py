from clawbox.experiments.host_kb import HostKnowledgeManager  # initialize upstream imports
from clawbox.experiments.knowledge.observation import compact_observation
from clawbox.experiments.knowledge.protocol import learning_records
from tool_resource.runtime_kb import _target_values


def test_compact_targets_keep_measured_zero_imputation_and_guest_source():
    call = {'command': 'sleep 1', 'eligible_for_kb': True, 'clauses': [
        {'bin': 'sleep', 'argv': ['sleep', '1'], 'ts_start': 2., 'ts_end': 3.,
         'latency_ms': 1000., 'provenance': {'cpu_time_ns': 0}}]}
    stats = dict(ts_start=2., ts_end=3., duration_ms=1000, cpu_time_seconds=0.,
        cpu_source='cgroup-v2-cpu.stat', cgroup_attachment_verified=True,
        memory_eligible=True, memory_baseline_bytes=100, memory_total_peak_bytes=100,
        memory_extra_peak_bytes=0, memory_environment_id='guest-1',
        memory_measurement='guest_memtotal_minus_memavailable', memory_timeline=[[2., 100], [2.1, 100]])
    payload = compact_observation(call=call, artifact={'cleanup': 'ok', 'collection_validity': 'valid'},
        stats=stats, repository='repo', session_id='pair', execution_id='exec', receipt='receipt',
        command=call['command'], exit_code=0)
    completed, rows = learning_records(payload)
    assert completed.memory_measurement == 'guest_memtotal_minus_memavailable'
    assert payload['call_provenance']['cpu_time_seconds'] == 'observed'
    assert payload['call_provenance']['memory_extra_peak_bytes'] == 'observed'
    assert payload['call_provenance']['cpu_peak_cores'] == 'imputed_zero'
    assert _target_values(completed)['cpu_peak_cores'] == 0
    assert rows[0].memory_extra_peak_bytes == 0
    assert 'memory_extra_peak_bytes' not in rows[0].imputed_targets
    assert 'cpu_peak_cores' in rows[0].imputed_targets
    assert 'cpu_time_seconds' not in rows[0].imputed_targets
    assert 'memory_timeline' not in str(payload)


def test_cancelled_execution_is_censored_without_timeout_label():
    payload = compact_observation(call={'command': 'sleep 1', 'clauses': []},
        artifact={}, stats={'ts_start': 1., 'ts_end': 2.}, repository='repo', session_id='pair',
        execution_id='exec', receipt='receipt', command='sleep 1', exit_code=130, cancelled=True)
    completed, _ = learning_records(payload)
    assert completed.censored
    assert not completed.confirmed_timeout
    assert completed.timeout_duration_lower_bound_ms is None


def test_collector_finalizes_in_memory_and_retains_only_when_requested(tmp_path):
    from clawbox.experiments.knowledge.collector import HostClauseTelemetryCollector
    def collector(name):
        return HostClauseTelemetryCollector.unavailable(repo='repo', artifact_path=tmp_path/name,
                                                        reason='test unavailable collector')
    value = collector('no-file.json').finalize(write_artifact=False)
    assert value['version'] == 2
    assert not (tmp_path/'no-file.json').exists()
    kept = collector('diagnostic.json').finalize(write_artifact=True)
    import json
    assert json.loads((tmp_path/'diagnostic.json').read_text()) == kept
