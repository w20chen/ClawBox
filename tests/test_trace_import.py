import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from clawbox.replay.import_trace import adapt_call, import_trace, LIST_DIRECTORY
from clawbox.replay.trace import load_trace


def call(name, **args):
    return {'id': 'call-1', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def test_edit_mapping_preserves_text_and_source():
    source = call('edit_file', path='/testbed/x', old_text='a\nb', new_text='c\nd', replace_all=False)
    before = copy.deepcopy(source)
    target = adapt_call(source)
    assert source == before
    assert target['function']['name'] == 'edit'
    assert json.loads(target['function']['arguments']) == {'path': '/testbed/x', 'oldText': 'a\nb', 'newText': 'c\nd'}
    with pytest.raises(ValueError, match='replace_all'):
        adapt_call(call('edit_file', path='x', old_text='a', new_text='b', replace_all=True))
    with pytest.raises(ValueError, match='Unsupported tool arguments'):
        adapt_call(call('read_file', path='x', offset=50))
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
    assert json.loads(action.output['tool_calls'][0]['function']['arguments'])['workdir'] == '/testbed'
    assert source.read_bytes() == original and report['model_calls'] == 1
    assert output.with_suffix('.import.json').exists()
    with pytest.raises(ValueError, match='already exists'):
        import_trace(source, output)


def test_failed_import_does_not_publish_partial_trace(tmp_path):
    source = tmp_path/'bad.jsonl'
    source.write_text(json.dumps({'schema_version': 6}))
    output = tmp_path/'out.jsonl'
    with pytest.raises(ValueError, match='schema-5'):
        import_trace(source, output)
    assert not output.exists()
