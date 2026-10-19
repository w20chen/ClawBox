import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from clawbox.replay.import_trace import adapt_call, import_trace, LIST_DIRECTORY
from clawbox.replay.import_corpus import prepare_corpus
from clawbox.replay.trace import load_trace


def call(name, **args):
    return {'id': 'call-1', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def test_edit_mapping_preserves_text_and_source():
    source = call('edit_file', path='/testbed/x', old_text='a\nb', new_text='c\nd', replace_all=False)
    before = copy.deepcopy(source)
    target = adapt_call(source)
    assert source == before
    assert target['function']['name'] == 'edit'
    assert json.loads(target['function']['arguments']) == {'path': '/workspace/x', 'oldText': 'a\nb', 'newText': 'c\nd'}
    with pytest.raises(ValueError, match='replace_all'):
        adapt_call(call('edit_file', path='x', old_text='a', new_text='b', replace_all=True))
    target = adapt_call(call('read_file', path='x', offset=50, limit=20))
    assert target['function']['name'] == 'read'
    assert json.loads(target['function']['arguments']) == {'path': 'x', 'offset': 50, 'limit': 20}
    with pytest.raises(ValueError, match='offset'):
        adapt_call(call('read_file', path='x', offset=0))
    with pytest.raises(ValueError, match='Unsupported replay tool'):
        adapt_call(call('unknown'))


def test_directory_listing_is_sorted_recursive_and_bounded(tmp_path):
    (tmp_path/'a').mkdir()
    (tmp_path/'a'/'child').write_text('x')
    (tmp_path/'b').write_text('x')
    def run(recursive, maximum):
        return subprocess.check_output([sys.executable, '-c', LIST_DIRECTORY, str(tmp_path), recursive, str(maximum)],
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, encoding='utf-8')
    assert [line[2:] for line in run('false', 20).splitlines()] == ['a', 'b']
    assert [line[2:] for line in run('true', 20).splitlines()] == ['a', 'a/child', 'b']
    limited = run('true', 1).splitlines()
    assert len(limited) == 2 and limited[-1] == '[truncated: max_entries reached]'


def test_import_roundtrip_keeps_timing_and_provenance(tmp_path):
    source = tmp_path/'original.jsonl'
    rows = [{'type': 'trace_metadata', 'trace_format_version': 5, 'benchmark': 'swe-rebench',
             'scaffold': 'openclaw', 'model': 'recorded-model', 'instance_id': 'case'},
            {'type': 'action', 'action_type': 'llm_call', 'action_id': 'llm-0', 'instance_id': 'case',
             'ts_start': 1, 'ts_end': 3, 'data': {'messages_in': [{'role': 'user', 'content': 'task'}],
             'raw_response': {'choices': [{'message': {'role': 'assistant', 'tool_calls': [
                 call('exec', command='printf hello', working_dir='/testbed', timeout=10)]}}]}}}]
    source.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    original = source.read_bytes()
    output = tmp_path/'replay.jsonl'
    report = import_trace(source, output)
    action, = load_trace(output)
    assert action.duration_s == 2
    args = json.loads(action.output['tool_calls'][0]['function']['arguments'])
    assert args['workdir'] == '/workspace'
    assert report['workspace_mapping'] == {'source': '/testbed', 'target': '/workspace'}
    assert source.read_bytes() == original and report['model_calls'] == 1
    assert output.with_suffix('.import.json').exists()
    with pytest.raises(ValueError, match='already exists'):
        import_trace(source, output)


def test_corpus_import_prefers_projects_and_writes_unique_replay_cases(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()

    def write_trace(name, instance_id, start):
        path = source_root / name
        rows = [
            {"type": "trace_metadata", "trace_format_version": 5,
             "benchmark": "swe-rebench", "scaffold": "openclaw",
             "model": "recorded-model", "instance_id": instance_id},
            {"type": "action", "action_type": "llm_call",
             "action_id": "llm-0", "instance_id": instance_id,
             "ts_start": start, "ts_end": start + 10,
             "data": {"messages_in": [{"role": "user", "content": instance_id}],
                      "raw_response": {"choices": [{"message": {
                          "role": "assistant", "content": "done",
                      }}]}}},
        ]
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    write_trace("other__owner__repo-1.trace.jsonl", "owner__repo-1", 1)
    write_trace("sqlglot-dev__tobymao__sqlglot-2.trace.jsonl",
                "tobymao__sqlglot-2", 2)
    write_trace("pennylane__PennyLaneAI__pennylane-3.trace.jsonl",
                "PennyLaneAI__pennylane-3", 3)
    write_trace("pennylane__PennyLaneAI__pennylane-4.trace.jsonl",
                "PennyLaneAI__pennylane-4", 4)
    report = prepare_corpus(source_root, tmp_path / "output", count=2)
    manifest = json.loads(Path(report["manifest"]).read_text())
    assert [case["case_id"] for case in manifest["tasks"]] == [
        "PennyLaneAI__pennylane-3", "tobymao__sqlglot-2",
    ]
    assert len({case["replay_trace_reference"] for case in manifest["tasks"]}) == 2
    assert all(case["metadata"]["recorded_duration_seconds"] == 10
               for case in manifest["tasks"])
    # Re-running resumes verified imports instead of rebuilding them.
    assert prepare_corpus(source_root, tmp_path / "output", count=2)["trace_count"] == 2


def test_corpus_import_skips_incompatible_traces_and_fills_requested_count(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()

    def write_trace(name, instance_id, tool_call=None):
        message = {"role": "assistant", "content": "done"}
        if tool_call is not None:
            message["tool_calls"] = [tool_call]
        rows = [
            {"type": "trace_metadata", "trace_format_version": 5,
             "benchmark": "swe-rebench", "scaffold": "openclaw",
             "model": "recorded-model", "instance_id": instance_id},
            {"type": "action", "action_type": "llm_call",
             "action_id": "llm-0", "instance_id": instance_id,
             "ts_start": 1, "ts_end": 2,
             "data": {"messages_in": [{"role": "user", "content": instance_id}],
                      "raw_response": {"choices": [{"message": message}]}}},
        ]
        (source_root / name).write_text(
            ''.join(json.dumps(row) + '\n' for row in rows)
        )

    write_trace(
        "pennylane-1.trace.jsonl", "PennyLaneAI__pennylane-1",
        call("edit_file", path="/testbed/a", old_text="x", new_text="y",
             replace_all=True),
    )
    write_trace("sqlglot-2.trace.jsonl", "tobymao__sqlglot-2")
    write_trace("sqlglot-3.trace.jsonl", "tobymao__sqlglot-3")

    report = prepare_corpus(source_root, tmp_path / "output", count=2)
    manifest = json.loads(Path(report["manifest"]).read_text())
    assert [case["case_id"] for case in manifest["tasks"]] == [
        "tobymao__sqlglot-2", "tobymao__sqlglot-3",
    ]
    assert report["skipped_count"] == 1
    assert manifest["skipped"][0]["source_reference"].endswith(
        "pennylane-1.trace.jsonl"
    )
    assert "replace_all=true" in manifest["skipped"][0]["reason"]


def test_failed_import_does_not_publish_partial_trace(tmp_path):
    source = tmp_path/'bad.jsonl'
    source.write_text(json.dumps({'schema_version': 6}))
    output = tmp_path/'out.jsonl'
    with pytest.raises(ValueError, match='schema-5'):
        import_trace(source, output)
    assert not output.exists()


def test_workspace_mapping_covers_commands_and_structured_paths():
    target = adapt_call(call(
        'exec',
        command=(
            'cd /testbed && cat /testbed2 /testbed/a '
            '/opt/conda/envs/testbed/bin/python'
        ),
        working_dir='/testbed/subdir',
    ))
    args = json.loads(target['function']['arguments'])
    assert args == {
        'command': (
            'cd /workspace && cat /testbed2 /workspace/a '
            '/opt/conda/envs/testbed/bin/python'
        ),
        'workdir': '/workspace/subdir',
    }


def test_import_maps_historical_tool_calls_in_model_inputs(tmp_path):
    source = tmp_path / 'source.jsonl'
    previous = call('exec', command='cat /testbed/a', working_dir='/testbed')
    rows = [
        {'type': 'trace_metadata', 'trace_format_version': 5,
         'benchmark': 'swe-rebench', 'scaffold': 'openclaw',
         'model': 'recorded-model', 'instance_id': 'case'},
        {'type': 'action', 'action_type': 'llm_call', 'action_id': 'llm-0',
         'instance_id': 'case', 'ts_start': 1, 'ts_end': 2,
         'data': {'messages_in': [
             {'role': 'assistant', 'tool_calls': [previous]},
         ], 'raw_response': {'choices': [{'message': {'role': 'assistant'}}]}}},
    ]
    source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    output = tmp_path / 'output.jsonl'
    import_trace(source, output)
    start = json.loads(output.read_text().splitlines()[1])
    args = json.loads(start['input']['messages'][0]['tool_calls'][0]['function']['arguments'])
    assert args == {'command': 'cat /workspace/a', 'workdir': '/workspace'}
