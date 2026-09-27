"""Explicit import of SWE-rebench research schema-5 traces for current OpenClaw.

The source remains unchanged. This is an offline adapter, not runtime fallback.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import shlex
import tempfile
from pathlib import Path

from .trace import _read_jsonl, load_trace
from clawbox.experiments.openclaw_driver import TOOL_VM_TOOLS, RUNTIME_LOCAL_TOOLS


LIST_DIRECTORY = """import os, sys
root, recursive, maximum = sys.argv[1], sys.argv[2] == 'true', int(sys.argv[3])
count = 0
def walk(directory, prefix=''):
    global count
    with os.scandir(directory) as iterator:
        entries = sorted(iterator, key=lambda entry: entry.name)
    for entry in entries:
        if count >= maximum:
            print('[truncated: max_entries reached]')
            return False
        relative = prefix + entry.name
        is_dir = entry.is_dir(follow_symlinks=False)
        print(('\U0001f4c1 ' if is_dir else '\U0001f4c4 ') + relative)
        count += 1
        if recursive and is_dir and not walk(entry.path, relative + '/'):
            return False
    return True
walk(root)
"""


def _fields(args, allowed, required):
    if not isinstance(args, dict) or set(args) - set(allowed) or set(required) - set(args):
        raise ValueError(f'Unsupported tool arguments: expected {allowed}, required {required}')


def adapt_call(call: dict, *, python: str = 'python3') -> dict:
    result = copy.deepcopy(call)
    function = result['function']
    name = function['name']
    args = function.get('arguments', {})
    if isinstance(args, str):
        args = json.loads(args)
    if name == 'edit_file':
        _fields(args, ('path', 'old_text', 'new_text', 'replace_all'), ('path', 'old_text', 'new_text'))
        if args.get('replace_all', False) is not False:
            raise ValueError('edit_file replace_all=true is not supported by this importer')
        name = 'edit'
        args = {'path': args['path'], 'oldText': args['old_text'], 'newText': args['new_text']}
    elif name == 'read_file':
        _fields(args, ('path',), ('path',))
        name = 'read'
    elif name == 'write_file':
        _fields(args, ('path', 'content'), ('path', 'content'))
        name = 'write'
    elif name == 'list_dir':
        _fields(args, ('path', 'recursive', 'max_entries'), ('path',))
        recursive, maximum = args.get('recursive', False), args.get('max_entries', 200)
        if type(recursive) is not bool or type(maximum) is not int or maximum < 1:
            raise ValueError('list_dir requires boolean recursive and positive integer max_entries')
        command = shlex.join([python, '-c', LIST_DIRECTORY, args['path'], str(recursive).lower(), str(maximum)])
        name, args = 'exec', {'command': command}
    elif name == 'exec':
        _fields(args, ('command', 'working_dir', 'workdir', 'timeout'), ('command',))
        if 'working_dir' in args:
            if 'workdir' in args:
                raise ValueError('exec contains both working_dir and workdir')
            args['workdir'] = args.pop('working_dir')
    elif name not in (*TOOL_VM_TOOLS, *RUNTIME_LOCAL_TOOLS):
        raise ValueError(f'Unsupported replay tool: {name}')
    function.update(name=name, arguments=json.dumps(args, ensure_ascii=True))
    return result


def import_trace(source: Path, output: Path, *, python: str = 'python3') -> dict:
    if output.exists() or output.with_suffix('.import.json').exists():
        raise ValueError('Import output already exists; choose a new output path')
    rows = _read_jsonl(source)
    metadata = [r for r in rows if r.get('type') == 'trace_metadata']
    if len(metadata) != 1 or metadata[0].get('trace_format_version') != 5:
        raise ValueError('This importer requires a SWE-rebench research schema-5 recording')
    meta = metadata[0]
    if meta.get('benchmark') != 'swe-rebench' or meta.get('scaffold') != 'openclaw':
        raise ValueError('Expected SWE-rebench OpenClaw research recording')
    spans = [r for r in rows if r.get('type') == 'action' and r.get('action_type') == 'llm_call']
    if not spans or len({r.get('instance_id') for r in spans}) != 1:
        raise ValueError('Import requires exactly one nonempty task recording')
    if len({r['action_id'] for r in spans}) != len(spans):
        raise ValueError('Duplicate LLM action IDs')
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    converted = [{'schema_version': 6, 'record_type': 'trace_metadata',
                  'importer': 'swe_rebench_research_v5', 'source_sha256': source_hash}]
    changes = []
    for index, span in enumerate(spans):
        data = span['data']
        response = data['raw_response']['choices']
        if len(response) != 1:
            raise ValueError('Expected one recorded model choice')
        message = copy.deepcopy(response[0]['message'])
        for i, call in enumerate(message.get('tool_calls', [])):
            adapted = adapt_call(call, python=python)
            if adapted != call:
                changes.append({'call_id': call['id'], 'source': call['function'], 'target': adapted['function']})
            message['tool_calls'][i] = adapted
        start, end = float(span['ts_start']), float(span['ts_end'])
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise ValueError('Invalid model timestamps')
        common = {'schema_version': 6, 'trace_id': source_hash, 'span_id': span['action_id'],
                  'kind': 'llm', 'name': meta.get('model', ''), 'sequence_no': index}
        converted.append({**common, 'record_type': 'span_start', 'wall_time_ns': str(round(start*1e9)),
                          'input': {'messages': data['messages_in']}})
        converted.append({**common, 'record_type': 'span_end', 'duration_ns': str(round((end-start)*1e9)),
                          'status': {'code': 'ok'}, 'output': {'content': message}})
    output.parent.mkdir(parents=True, exist_ok=True)
    # Validate through the same reader used by replay before publishing the file.
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix='.jsonl', delete=False) as handle:
        temporary = Path(handle.name)
    try:
        temporary.write_text(''.join(json.dumps(r, ensure_ascii=True)+'\n' for r in converted), encoding='utf-8', newline='\n')
        load_trace(temporary)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    report = {'importer': 'swe_rebench_research_v5', 'source': str(source.resolve()),
              'source_sha256': source_hash, 'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
              'model': meta.get('model'), 'instance_id': meta.get('instance_id'),
              'source_runtime': meta.get('prompt_runtime_label'), 'model_calls': len(spans),
              'changes': changes,
              'limitations': ['Directory listing is sorted, bounded, and does not follow directory symlinks.',
                              'Tool output text and architecture are not guaranteed identical to the source.',
                              'Recorded model decisions are fixed; validate the final task in the target VM.']}
    output.with_suffix('.import.json').write_text(json.dumps(report, indent=2), encoding='utf-8', newline='\n')
    return {k: v for k, v in report.items() if k != 'changes'}
