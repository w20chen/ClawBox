from dataclasses import replace
import json

from clawbox.experiments.knowledge.host import SharedSeed
from clawbox.experiments.knowledge.layers import OverlayMap, SharedSequence
from clawtune_kb.store import create_seed
from tool_resource.runtime_kb import RuntimeToolResourceKB, ClauseResourceKB, CompletedCall, ToolCallQuery
from tool_time.lattice_kb import LatticeTimeKB, LATTICE_TIME_ALGORITHMS
def observation(index=0, **kwargs):
    from tool_resource.runtime_kb import ClauseObservation
    values = dict(repo='org/repo', bin='python', argv=('python', 'work.py'), ts_start=float(index),
        ts_end=float(index+1), latency_ms=1000., cpu_ns_cumulative=2000000000, cpu_peak_cores=3.,
        sampled_peak_rss_mb=256., memory_total_peak_bytes=512*1024**2,
        memory_extra_peak_bytes=512*1024**2, memory_eligible=True,
        memory_measurement='cgroup_v2_environment_union_v1')
    values.update(kwargs)
    return ClauseObservation(**values)


def seed_at(tmp_path):
    rows = [observation(i, argv=('python', '-m', 'pytest', '-q', *(['tests/a.py'] if i else [])))
            for i in range(3)]
    calls = [CompletedCall('org/repo', 'exec', 'python -m pytest -q', i, i + 1) for i in range(3)]
    tool = RuntimeToolResourceKB.fit_public(calls)
    trie = ClauseResourceKB.fit_public(rows)
    for call in calls:
        tool.observe_completed_call(call)
    for row in rows:
        trie.observe_completed_clause(row)
    tool._advance(4.)
    trie._advance(4.)
    lattice = LatticeTimeKB.fit(rows)
    create_seed(tmp_path / 'seed', {
        'runtime-tool-resource-kb.json': tool.to_json_obj(),
        'clause-resource-kb.json': trie.to_json_obj(),
        'clause-lattice-time-kb.json': lattice.to_json_obj(),
    }, provenance={'test': True})
    return SharedSeed(tmp_path / 'seed')


def test_layered_models_equal_full_copies_and_isolate_pairs(tmp_path):
    seed = seed_at(tmp_path)
    first, other = seed.open(), seed.open()
    tool = RuntimeToolResourceKB.from_json_obj(seed.tool.to_json_obj())
    trie = ClauseResourceKB.from_json_obj(seed.trie.to_json_obj())
    lattice = LatticeTimeKB.from_json_obj(seed.lattice.to_json_obj())
    untouched = json.dumps(seed.lattice.to_json_obj(), sort_keys=True)
    for i, argv in enumerate((('python', '-m', 'pytest', '-q'),
                              ('python', '-m', 'pytest', '-q', '-x'),
                              ('python', '-m', 'pytest', '-q', 'tests/a.py'))):
        row = observation(20 + i * 10, argv=argv, latency_ms=8000 + i * 100,
                          memory_total_peak_bytes=0, imputed_targets=('memory_total_peak_bytes',))
        call = CompletedCall('org/repo', 'exec', 'python -m pytest -q', row.ts_start, row.ts_end)
        first = first.successor(call, [row])
        # Match the host's causal translation, retaining duration differences.
        translated = first.maintenance.rows[-1]
        tool.observe_completed_call(replace(call, ts_start=translated.ts_start, ts_end=translated.ts_end))
        trie.observe_completed_clause(translated)
        lattice.observe_completed_clause(translated)
        query = ToolCallQuery('org/repo', 'exec', call.command, 1000.)
        assert first.tool.predict_load_samples(query) == tool.predict_load_samples(query)
        for query_argv in (argv, (*argv, '--verbose')):
            clauses = [{'bin': 'python', 'argv': list(query_argv)}]
            assert first.trie.predict_load_samples('org/repo', clauses, 1000.) == trie.predict_load_samples('org/repo', clauses, 1000.)
            for algorithm in LATTICE_TIME_ALGORITHMS:
                assert first.lattice.predict_load_samples('org/repo', clauses, 1000., algorithm=algorithm) == lattice.predict_load_samples('org/repo', clauses, 1000., algorithm=algorithm)
            assert first.lattice.predict_resource_clauses('org/repo', clauses, 1000.) == lattice.predict_resource_clauses('org/repo', clauses, 1000.)
        assert other.generation == 0
        assert json.dumps(seed.lattice.to_json_obj(), sort_keys=True) == untouched
    assert first.generation == 3
    assert isinstance(first.maintenance.rows, SharedSequence)
    assert first.maintenance.rows.base is seed.maintenance.rows
    assert isinstance(first.maintenance.views['time'].raw_nodes, OverlayMap)


def test_1280_empty_scopes_share_seed_evidence(tmp_path):
    seed = seed_at(tmp_path)
    scopes = [seed.open() for _ in range(1280)]
    assert all(s.maintenance is seed.maintenance for s in scopes)
    assert all(s.tool._public is seed.tool._public for s in scopes)
    assert all(s.trie._repo.base is seed.trie._repo for s in scopes)
