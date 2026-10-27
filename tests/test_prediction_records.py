from clawbox.experiments.prediction_records import PredictionRecordBuffer


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
