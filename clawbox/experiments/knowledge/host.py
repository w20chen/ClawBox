"""Host-owned seed and private views over the three current ClawTune models.

No service, authentication or experiment policy lives here. The owner serializes
each scope's transactions and publishes a prepared successor after durable commit.
"""
from collections.abc import Mapping
from copy import copy, deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path

from clawbox.experiments.knowledge.layers import SharedSequence
from clawtune_kb.store import load_seed
from tool_resource.runtime_kb import RuntimeToolResourceKB, ClauseResourceKB
from tool_time.lattice_kb import LatticeTimeKB, _sanitize_resources, _eligible_observation


class _RepositoryView(Mapping):
    def __init__(self, base, delta, depth=3):
        self.base, self.delta, self.depth = base, delta, depth

    def __getitem__(self, key):
        if key not in self.delta:
            return self.base[key]
        if key not in self.base:
            return self.delta[key]
        if self.depth == 1:
            return SharedSequence(self.base[key], self.delta[key])
        return type(self)(self.base[key], self.delta[key], self.depth - 1)

    def __iter__(self):
        yield from self.base
        yield from (key for key in self.delta if key not in self.base)

    def __len__(self):
        return len(self.base) + sum(key not in self.base for key in self.delta)


class SharedSeed:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.manifest, payloads = load_seed(self.path)
        self.digest = hashlib.sha256((self.path / 'manifest.json').read_bytes()).hexdigest()
        self.tool = RuntimeToolResourceKB.from_json_obj(payloads['runtime-tool-resource-kb.json'])
        self.trie = ClauseResourceKB.from_json_obj(payloads['clause-resource-kb.json'])
        self.lattice = LatticeTimeKB.from_json_obj(payloads['clause-lattice-time-kb.json'])
        for kb in (self.tool, self.trie, self.lattice):
            kb.freeze()
        from .incremental import IncrementalLatticeState
        self.maintenance = IncrementalLatticeState(self.lattice._maintenance.rows,
            self.lattice._maintenance.state(), maximum=self.lattice.max_optional_features,
            subset_coverage=self.lattice._subset_coverage)
        self.last_time = max((row.ts_end for row in self.lattice._observations), default=0.)

    def open(self):
        return PrivateKnowledge(self)


class PrivateKnowledge:
    def __init__(self, seed):
        self.seed = seed
        self.generation = 0
        self.last_time = seed.last_time
        self.tool_delta = RuntimeToolResourceKB()
        self.trie_delta = ClauseResourceKB()
        self.maintenance = seed.maintenance
        self._install_views()

    def _install_views(self):
        self.tool = copy(self.seed.tool)
        self.tool._repo = _RepositoryView(self.seed.tool._repo, self.tool_delta._repo)
        self.trie = copy(self.seed.trie)
        self.trie._repo = _RepositoryView(self.seed.trie._repo, self.trie_delta._repo)
        self.lattice = copy(self.seed.lattice)
        self.lattice._install_node_state(self.maintenance.state())
        # Frozen means query-time absorption is disabled. Scope publication,
        # rather than guest clocks, controls visibility of committed evidence.
        self.lattice._frozen = True

    def successor(self, call, clauses):
        """Prepare without mutating this generation or the shared seed.

        Translate all timestamps by one offset, retaining actual differences.
        Host ordering places each execution after previous committed evidence;
        guest clock resets cannot backdate it into a prior generation.
        """
        result = copy(self)
        result.tool_delta = deepcopy(self.tool_delta)
        result.trie_delta = deepcopy(self.trie_delta)
        times = [r.ts_start for r in clauses]
        if call is not None:
            times.append(call.ts_start)
        offset = self.last_time + 1. - min(times) if times else 0.
        shifted = tuple(replace(row, ts_start=row.ts_start + offset, ts_end=row.ts_end + offset)
                        for row in clauses)
        if call is not None:
            call = replace(call, ts_start=call.ts_start + offset, ts_end=call.ts_end + offset)
            result.tool_delta.observe_completed_call(call)
            result.tool_delta._absorb_completed(float('inf'))
        accepted = []
        for row in shifted:
            result.trie_delta.observe_completed_clause(row)
            clean = _sanitize_resources(row)
            if _eligible_observation(clean):
                accepted.append(clean)
        result.trie_delta._absorb_completed(float('inf'))
        if accepted:
            result.maintenance = self.maintenance.extend(
                SharedSequence(self.maintenance.rows, accepted), shared=True)
        result.last_time = max([self.last_time, *(r.ts_end for r in shifted),
                                *([call.ts_end] if call is not None else [])])
        result.generation += 1
        result._install_views()
        return result

    def predict(self, query, *, command_context=None, features=None, feature_call_id=None):
        from clawtune_sidecar.predictors.call_load import predict_call_load
        from clawtune_sidecar.prediction_config import load_bucket_edges
        from tool_time.query import LatticeQueryCache
        return predict_call_load(runtime=self.tool, trie=self.trie, lattice=self.lattice,
            query=query, edges=load_bucket_edges((100., 500., 2000., 10000.)),
            feature_service=features, feature_call_id=feature_call_id,
            command_context=command_context, query_cache=LatticeQueryCache())
