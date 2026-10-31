"""Version-one host KB wire validation and canonical identities."""
import hashlib
import json
from dataclasses import fields

from functools import lru_cache
from pathlib import Path
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from clawtune_kb.contracts import data_root


@lru_cache(maxsize=4)
def _validator(name):
    schemas = {}
    for root in (data_root() / 'contracts', Path(__file__).parent / 'contracts'):
        for path in root.glob('*.schema.json'):
            schema = json.loads(path.read_text(encoding='utf-8'))
            schemas[path.name] = schema
    registry = Registry().with_resources((s['$id'], Resource.from_contents(s)) for s in schemas.values())
    return Draft202012Validator(schemas[name], registry=registry)

PROTOCOL = 'clawtune.host-kb.v1'
MAX_BODY_BYTES = 1024 * 1024


def canonical(value):
    # All implementations hash these exact UTF-8 bytes; NaN/Infinity are invalid.
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def command_digest(command):
    return hashlib.sha256(command.encode('utf-8')).hexdigest()


def validate_message(value, kind):
    encoded = canonical(value)
    if len(encoded) > MAX_BODY_BYTES:
        raise ValueError('host KB request exceeds 1048576 bytes')
    try:
        _validator(f'host-kb-{kind}.schema.json').validate(json.loads(encoded))
    except ValidationError as exc:
        raise ValueError(f'invalid host KB {kind}: {exc.message}') from exc
    if kind in {'predict', 'observe'} and command_digest(value['command']) != value['command_sha256']:
        raise ValueError('logical command digest mismatch')


def learning_records(payload):
    from tool_resource.runtime_kb import CompletedCall, ClauseObservation
    def record(cls, data):
        if data is None:
            return None
        unknown = set(data) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f'unknown learning fields: {sorted(unknown)}')
        data = dict(data)
        for name in ('argv', 'imputed_targets'):
            if name in data:
                data[name] = tuple(data[name])
        return cls(**data)
    return record(CompletedCall, payload['call']), tuple(record(ClauseObservation, row) for row in payload['clauses'])
