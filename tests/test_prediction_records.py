from clawbox.experiments.prediction_records import (
    PredictionRecordBuffer,
    compact_prediction_records,
)
from clawbox.experiments.worker import summarize_tool_execution_observations


def test_completed_evidence_is_encoded_and_materializes_in_admission_order():
    records = PredictionRecordBuffer()
    original = {'execution_id': 'a', 'models': {'lattice': {'buckets': [1, 2]}}}
    records.append(original)
    records.append({'execution_id': 'b', 'actual_measured_memory_mib': None})
    records.complete('b', {'host_cgroup_execution_increment_bytes': None})
    records.complete('a', {'host_cgroup_execution_increment_bytes': 2 * 1024**2})
    assert all(isinstance(row, bytes) for row in records._rows)
    assert records._active == {}
    result = records.records()
    assert [r['execution_id'] for r in result] == ['a', 'b']
    assert result[0]['actual_host_execution_increment_mib'] == 2
    assert result[1]['actual_host_execution_increment_mib'] is None
    assert result[0]['models'] == original['models']
    result[0]['models']['lattice']['buckets'].append(3)
    assert records.records()[0]['models']['lattice']['buckets'] == [1, 2]


def test_incomplete_evidence_remains_explicitly_incomplete():
    records = PredictionRecordBuffer()
    records.append({'execution_id': 'pending', 'actual_measured_memory_mib': None})
    assert records.records() == [{'execution_id': 'pending', 'actual_measured_memory_mib': None}]


def test_large_repeated_evidence_is_retained_compactly_without_truncation():
    records = PredictionRecordBuffer()
    text = 'repeated diagnostic evidence\n' * 10000
    records.append({'execution_id': 'large', 'evidence': text})
    records.complete('large', {'host_cgroup_execution_increment_bytes': None})
    assert len(records._rows[0]) < len(text) / 10
    assert records.records()[0]['evidence'] == text


def test_archive_compaction_keeps_analysis_fields_and_digests_model_detail():
    record = {
        'execution_id': 'a',
        'execution_scope': 'agent-tool',
        'prediction_source': 'runtime_clawtune_immutable_kb',
        'fallback_level': 'lattice',
        'predicted_guest_memory_p90_mib': 12.0,
        'actual_measured_memory_mib': 10.0,
        'prediction_error_mib': 2.0,
        'prediction_absolute_error_mib': 2.0,
        'prediction_underestimate_mib': 0.0,
        'prediction_covered_actual': True,
        'telemetry_validity': 'valid',
        'models': {'lattice': {'histogram': list(range(100))}},
        'call_prediction': {'load': {'targets': {'memory': {'p90': 12}}}},
        'session_runtime_metadata': {'prediction_receipt': 'receipt-a'},
    }
    compact = compact_prediction_records([record])
    archived = compact[0]
    for name in ('models', 'call_prediction', 'session_runtime_metadata'):
        assert name not in archived
        assert len(archived[f'{name}_sha256']) == 64
    assert summarize_tool_execution_observations(compact) == (
        summarize_tool_execution_observations([record])
    )
