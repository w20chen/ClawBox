import json

from clawbox.experiments.priority import PriorityDomain


def test_prepare_publishes_only_native_placement_intent(tmp_path):
    registry = tmp_path/'registry'
    registry.mkdir()
    domain = PriorityDomain(
        tmp_path, concurrency=1, repetition=0, legacy=False,
        registry=registry, placements={0: ('0-3', 0)},
    )
    stale = tmp_path/'stale-leaf'
    domain.leaves[0, 'tool'] = stale
    PriorityDomain.prepare(domain, 0, 'tool')
    assert json.loads((registry/domain.token(0, 'tool')).read_text()) == {'numa_node': 0}
    assert (0, 'tool') not in domain.leaves
