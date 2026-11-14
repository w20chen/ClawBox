import json
from pathlib import Path

import pytest

from clawbox.experiments.serving_sim_inputs import (
    load_serving_sim_manifest, prepare_serving_sim, serving_sim_case_binding,
    serving_sim_input_identity,
)
from clawbox.experiments.spec import load_experiment
from clawbox.experiments.spec_types import InferenceBackend, WorkloadSource
from clawbox.replay.import_trace import import_trace


def prepared_source(tmp_path):
    source = tmp_path / 'original.jsonl'
    rows = [dict(type='trace_metadata', trace_format_version=5, benchmark='swe-rebench',
                 scaffold='openclaw', model='recorded-model', instance_id='case')]
    # Deliberately inverse iterations: the CPU's selected action sequence wins.
    for index in range(2):
        message = dict(role='assistant', content='done') if index else dict(
            role='assistant', content=None, tool_calls=[dict(id='tool-0', type='function',
            function=dict(name='read_file', arguments=json.dumps(dict(path='/testbed/a'))))])
        rows.append(dict(type='action', action_type='llm_call', action_id=f'llm-{index}',
                         instance_id='case', agent_id='recorded-agent', iteration=1-index,
                         ts_start=index*2, ts_end=index*2+1,
                         data=dict(messages_in=[dict(role='user', content='task')],
                                   completion_tokens=12+index,
                                   raw_response=dict(choices=[dict(message=message,
                                       finish_reason='stop' if index else 'tool_calls')]))))
    source.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
    replay = tmp_path / 'replay.jsonl'
    import_trace(source, replay)
    spec = load_experiment(Path('examples/experiments/getting-started.yaml'))
    case = spec.workload.cases[0].model_copy(update=dict(
        case_id='case', source_reference=str(source), replay_trace_reference=str(replay)))
    spec = spec.model_copy(update={'workload': spec.workload.model_copy(update={'cases': (case,)})})
    return source, replay, spec


def test_bundle_preserves_token_evidence_and_cpu_tool_contract(tmp_path):
    source, replay, spec = prepared_source(tmp_path)
    original = source.read_bytes()
    report = prepare_serving_sim(spec, tmp_path / 'bundle')
    manifest = load_serving_sim_manifest(report['manifest'], verify=True)
    binding = serving_sim_case_binding({'serving_sim_catalog_manifest': report['manifest']}, 'case')
    provenance = json.loads((tmp_path / 'bundle/gpu-provenance.json').read_text())['cases'][0]
    rows = [json.loads(row) for row in (tmp_path / 'bundle' / provenance['gpu_trace_path']).read_text().splitlines()]
    assert provenance['action_ids'] == ['llm-0', 'llm-1']
    assert binding['missing_reasoning_calls'] == 2
    assert binding['source_models'] == ['recorded-model']
    assert [row['iteration'] for row in rows[1:]] == [0, 1]
    assert [row['clawbox_source_iteration'] for row in rows[1:]] == [1, 0]
    tool = rows[1]['data']['raw_response']['choices'][0]['message']['tool_calls'][0]['function']
    assert tool['name'] == 'read'
    assert json.loads(tool['arguments'])['path'] == '/workspace/a'
    assert [row['data']['completion_tokens'] for row in rows[1:]] == [12, 13]
    assert all('reasoning_output_tokens' not in row['data'] for row in rows[1:])
    assert manifest['catalog']['catalog_digest'] == report['catalog_digest']
    assert source.read_bytes() == original
    with pytest.raises(ValueError, match='already exists'):
        prepare_serving_sim(spec, tmp_path / 'bundle')
    replay.unlink()
    source.unlink()
    assert load_serving_sim_manifest(report['manifest'], verify=True) == manifest


@pytest.mark.parametrize('field,value,error', [
    ('completion_tokens', None, 'completion_tokens'),
    ('reasoning_output_tokens', -1, 'reasoning count'),
])
def test_missing_or_invalid_lengths_do_not_publish_bundle(tmp_path, field, value, error):
    source, replay, spec = prepared_source(tmp_path)
    rows = [json.loads(row) for row in source.read_text().splitlines()]
    rows[1]['data'][field] = value
    source.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    # Reimport modified evidence so the separate source-hash gate does not mask
    # the missing length validation exercised here.
    updated = tmp_path / 'new-replay.jsonl'
    import_trace(source, updated)
    case = spec.workload.cases[0].model_copy(update={'replay_trace_reference': str(updated)})
    spec = spec.model_copy(update={'workload': spec.workload.model_copy(update={'cases': (case,)})})
    with pytest.raises(ValueError, match=error):
        prepare_serving_sim(spec, tmp_path / 'bundle')
    assert not (tmp_path / 'bundle').exists()


def test_source_map_and_input_identity_reject_wrong_workload(tmp_path):
    source, replay, spec = prepared_source(tmp_path)
    case = spec.workload.cases[0].model_copy(update={'source_reference': str(replay)})
    spec = spec.model_copy(update={'workload': spec.workload.model_copy(update={'cases': (case,)})})
    with pytest.raises(ValueError, match='source-map'):
        prepare_serving_sim(spec, tmp_path / 'missing')
    report = prepare_serving_sim(spec, tmp_path / 'bundle', source_map={'case': str(source)})
    config = dict(model='sim-model', base_url='http://localhost:8000', serving_sim_catalog_manifest=report['manifest'])
    spec = spec.model_copy(update={'inference': spec.inference.model_copy(update={
        'backend': InferenceBackend.SERVING_SIM, 'configuration': config})})
    assert serving_sim_input_identity(spec)['serving_sim']['catalog_digest'] == report['catalog_digest']
    from clawbox.experiments.inputs import validate_inputs
    assert validate_inputs(spec)['serving_sim']['catalog_digest'] == report['catalog_digest']
    from clawbox.experiments.qualification import qualification_spec, qualification_input_manifest, current_qualification_input_manifest
    qualified = qualification_spec(spec)
    assert qualified.inference.backend is InferenceBackend.SERVING_SIM
    assert qualified.inference.configuration.get('time_scale') is None
    assert qualified.inference.configuration['max_model_steps'] == 1
    assert qualification_input_manifest(spec)['serving_sim'] == current_qualification_input_manifest(spec)['serving_sim']
    with pytest.raises(ValueError, match='absent'):
        serving_sim_case_binding(config, 'different-case')


def test_source_hash_mismatch_is_not_guessed_away(tmp_path):
    source, replay, spec = prepared_source(tmp_path)
    source.write_text(source.read_text()+'\n')
    with pytest.raises(ValueError, match='source hash differs'):
        prepare_serving_sim(spec, tmp_path / 'bundle')


def test_cpu_validation_and_qualification_need_no_trajectory_files(tmp_path, monkeypatch):
    from clawbox.experiments.inputs import validate_inputs, validate_launch_inputs
    from clawbox.experiments.qualification import qualification_input_manifest, current_qualification_input_manifest
    from clawbox.experiments.spec import load_workload_cases
    source, replay, spec = prepared_source(tmp_path)
    report = prepare_serving_sim(spec, tmp_path / 'bundle')
    cpu = tmp_path / 'cpu'
    cpu.mkdir()
    for name in ('manifest.json', 'workload.json'):
        (cpu / name).write_bytes((tmp_path / 'bundle' / name).read_bytes())
    spec = spec.model_copy(update={
        'inference': spec.inference.model_copy(update={'backend': InferenceBackend.SERVING_SIM,
            'configuration': dict(model='sim', base_url='http://localhost:8000',
                                  serving_sim_catalog_manifest=str(cpu / 'manifest.json'))}),
        'workload': spec.workload.model_copy(update={'source': WorkloadSource.SWE_REBENCH,
            'input': str(cpu / 'workload.json'), 'cases': ()})})
    source.unlink(); replay.unlink()
    for path in (tmp_path / 'bundle/traces').glob('*.jsonl'):
        path.unlink()
    (tmp_path / 'bundle/catalog.json').unlink()
    def forbidden(*args, **kwargs):
        raise AssertionError('CPU runtime attempted to read a trajectory')
    monkeypatch.setattr('clawbox.experiments.inputs.load_trace', forbidden)
    monkeypatch.setattr('clawbox.experiments.serving_sim_inputs.load_trace', forbidden)
    assert all(case.replay_trace_reference is None for case in load_workload_cases(spec.workload))
    assert validate_inputs(spec)['traces'] == []
    assert validate_launch_inputs(spec)['traces'] == []
    assert qualification_input_manifest(spec) == current_qualification_input_manifest(spec)
    cases = load_workload_cases(spec.workload)
    changed = spec.model_copy(update={'workload': spec.workload.model_copy(update={
        'cases': (cases[0].model_copy(update={'prompt': 'different task'}),)})})
    with pytest.raises(ValueError, match='CPU task differs'):
        validate_inputs(changed)
