"""Native validation retains the wire contract, including nested predictions."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from clawbox.experiments.knowledge import protocol
from clawtune_kb.contracts import data_root
from test_host_kb import setup_manager, prediction_payload


def reference(kind):
    schemas = {p.name: json.loads(p.read_text(encoding='utf-8'))
               for root in (data_root()/'contracts', Path(protocol.__file__).parent/'contracts')
               for p in root.glob('*.schema.json')}
    registry = Registry().with_resources((s['$id'], Resource.from_contents(s)) for s in schemas.values())
    return Draft202012Validator(schemas[f'host-kb-{kind}.schema.json'], registry=registry)


def test_native_and_reference_prediction_contracts_agree(tmp_path):
    manager = setup_manager(tmp_path)()
    manager.reserve('a', 'repo')
    response = manager.predict('a', prediction_payload())
    old, new = reference('prediction'), protocol._validator('host-kb-prediction.schema.json')
    assert old.is_valid(response) and new.is_valid(response)
    variants = []
    for field in ('schema_version', 'seed_sha256', 'mapping_policy_sha256',
                  'scope_generation', 'view_revision', 'prediction_receipt', 'prediction'):
        row = deepcopy(response); row.pop(field); variants.append(row)
        for value in (None, [], False, -1, 'invalid'):
            row = deepcopy(response); row[field] = value; variants.append(row)
    for backend in ('tool', 'trie', 'lattice'):
        for field, value in [('scope', 'invalid'), ('memory_measurement', 10),
                             ('schema_version', 'invalid'), ('targets', {}), ('unexpected', 1)]:
            row = deepcopy(response); row['prediction'][backend]['load'][field] = value; variants.append(row)
        for field, value in [('status', 'invalid'), ('p50', '1'), ('backend', 'invalid'),
                             ('unit', None), ('buckets', []), ('evidence_counts', False)]:
            row = deepcopy(response)
            row['prediction'][backend]['load']['targets']['duration_ms'][field] = value
            variants.append(row)
    # Extensible envelopes remain accepted; nested strict contracts remain strict.
    row = deepcopy(response); row['extra_metadata'] = {'future': True}; variants.append(row)
    rejected = 0
    for row in variants:
        expected = old.is_valid(row)
        assert new.is_valid(row) == expected
        if not expected:
            rejected += 1
            with pytest.raises(ValueError, match='invalid host KB prediction'):
                protocol.validate_message(row, 'prediction')
    assert rejected >= 60


def test_native_request_validation_preserves_size_digest_and_json_rules():
    body = prediction_payload()
    old, new = reference('predict'), protocol._validator('host-kb-predict.schema.json')
    protocol.validate_message(body, 'predict')
    for field in body:
        row = deepcopy(body); row.pop(field)
        assert old.is_valid(row) == new.is_valid(row)
    for change, message in [({'command_sha256': '0'*64}, 'digest mismatch'),
                            ({'raw_params': {'data': 'a' * protocol.MAX_BODY_BYTES}}, 'exceeds'),
                            ({'raw_params': {'data': float('nan')}}, 'Out of range')]:
        with pytest.raises(ValueError, match=message):
            protocol.validate_message({**body, **change}, 'predict')
