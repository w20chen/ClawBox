"""Attempt-local host knowledge, durable observations and prediction receipts."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from functools import wraps
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clawbox.clawtune_integration import use_clawtune

use_clawtune()
from clawbox.experiments.knowledge.host import SharedSeed
from clawbox.experiments.knowledge.protocol import PROTOCOL, canonical, digest, learning_records, validate_message
from command_parser import CommandFeatureService, ParseContext
from command_parser.registry import RuleRegistry
from tool_resource.runtime_kb import ToolCallQuery, training_target_provenance


def _sync_directory(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_identity(path, value):
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ValueError(f'host KB identity changed at {path}; use the original configuration or a new run ID')
        return
    with path.open('xb') as stream:
        stream.write(canonical(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


@dataclass
class _Scope:
    identity: dict
    path: Path
    view: Any
    features: Any
    lock: Any = field(default_factory=threading.RLock)
    predictions: dict = field(default_factory=dict)
    receipts: dict = field(default_factory=dict)
    claims: dict = field(default_factory=dict)
    committed: dict = field(default_factory=dict)
    degraded: bool = False


def _measured(operation):
    @wraps(operation)
    def measured(self, session_id, payload):
        started, cpu = time.monotonic(), time.thread_time()
        record = {'operation': operation.__name__, 'session_id': session_id,
                  'started_monotonic': started}
        try:
            result = operation(self, session_id, payload)
            record['status'] = result.get('status', 'ok')
            return result
        except Exception as exc:
            record.update(status='error', error_type=type(exc).__name__)
            raise
        finally:
            record.update(seconds=time.monotonic() - started, cpu_seconds=time.thread_time() - cpu)
            self.metrics.append(record)
    return measured


class HostKnowledgeManager:
    def __init__(self, seed_path, root, *, run_id, attempt_id, arm_id,
                 clawtune_revision, metadata, memory_measurement='cgroup_v2_environment_union_v1'):
        self.metrics = []
        self.seed = SharedSeed(Path(seed_path))
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.metadata = metadata
        self.measurement = memory_measurement
        self.policy_digest = digest({'policy': 'pair', 'version': 1})
        self.identity = dict(schema_version=PROTOCOL, run_id=run_id, attempt_id=attempt_id,
            arm_id=arm_id, seed_sha256=self.seed.digest, mapping_policy_sha256=self.policy_digest,
            clawtune_revision=clawtune_revision, memory_measurement=memory_measurement,
            label_policy='clawtune_marked_imputation', max_optional_features=self.seed.lattice.max_optional_features)
        _write_identity(self.root / 'manifest.json', self.identity)
        self.scopes = {}
        self.lock = threading.RLock()
        self.registry = RuleRegistry(read_only=True)

    def reserve(self, session_id, repository):
        identity = {**self.identity, 'session_id': session_id, 'repository': repository}
        with self.lock:
            if session_id in self.scopes:
                scope = self.scopes[session_id]
                if scope.identity != identity:
                    raise ValueError('session binding changed')
                return
            path = self.root / 'scopes' / digest(identity)
            path.mkdir(parents=True, exist_ok=True)
            _write_identity(path / 'identity.json', identity)
            scope = _Scope(identity, path, self.seed.open(), CommandFeatureService(self.registry))
            journal = path / 'journal.jsonl'
            if journal.exists():
                with journal.open('r+b') as stream:
                    while line := stream.readline():
                        if not line.endswith(b'\n'):
                            # Only a torn final append is discardable.
                            stream.truncate(stream.tell() - len(line))
                            stream.flush()
                            os.fsync(stream.fileno())
                            break
                        self._replay(scope, json.loads(line))
            self.scopes[session_id] = scope

    def _append(self, scope, row):
        journal = scope.path / 'journal.jsonl'
        created = not journal.exists()
        with journal.open('a+b') as stream:
            start = stream.tell()
            try:
                stream.write(canonical(row) + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.truncate(start)
                stream.flush()
                os.fsync(stream.fileno())
                raise
        if created:
            _sync_directory(scope.path)

    def _replay(self, scope, row):
        kind = row['kind']
        if kind == 'claim':
            execution_id = row['execution_id']
            if execution_id in scope.claims:
                raise ValueError('duplicate execution claim in KB journal')
            scope.claims[execution_id] = row
        elif kind == 'observe':
            payload = row['payload']
            validate_message(payload, 'observe')
            call, clauses = learning_records(payload)
            execution_id = payload['execution_id']
            if execution_id in scope.committed or execution_id not in scope.claims:
                raise ValueError('inconsistent KB observation journal')
            if digest(payload) != row['payload_digest']:
                raise ValueError('KB observation digest mismatch')
            scope.view = scope.view.successor(call, clauses)
            if scope.view.generation != row['generation']:
                raise ValueError('KB journal generation mismatch')
            scope.committed[execution_id] = (row['wire_digest'], row['generation'])
        elif kind == 'degraded':
            scope.degraded = True
        else:
            raise ValueError(f'unknown KB journal record: {kind}')

    def _scope(self, session_id, payload):
        scope = self.scopes[session_id]
        if payload['session_id'] != session_id or payload['repository'] != scope.identity['repository']:
            raise ValueError('host KB binding mismatch')
        if payload['memory_measurement'] != self.measurement:
            raise ValueError('host KB measurement mismatch')
        return scope

    @_measured
    def predict(self, session_id, payload):
        validate_message(payload, 'predict')
        scope = self._scope(session_id, payload)
        key = payload['tool_call_id'] or payload['event_id']
        request_digest = digest(payload)
        with scope.lock:
            if key in scope.predictions:
                previous = scope.predictions[key]
                if previous['request_digest'] != request_digest:
                    raise ValueError('prediction call identity reused with different inputs')
                return previous['response']
            if any(c['call_key'] == key for c in scope.claims.values()):
                raise ValueError('call already claimed; reconcile execution before resuming')
            context = dict(payload['command_context'])
            if context.get('repo') not in (None, payload['repository']):
                raise ValueError('parser repository does not match binding')
            context['repo'] = payload['repository']
            # Pure POSIX guest identity; never read host files using guest paths.
            context['container_id'] = 'host-kb-guest-context'
            context['host_workspace'] = None
            query = ToolCallQuery(payload['repository'], payload['tool_name'], payload['command'],
                                  scope.view.last_time + 1., self.measurement)
            tool, diagnostics = scope.view.predict(query, command_context=ParseContext(**context),
                                                   features=scope.features, feature_call_id=key)
            models = {'tool': tool.model_dump(mode='json'),
                      **{name: diagnostics.backends[name].model_dump(mode='json') for name in ('trie', 'lattice')}}
            from dataclasses import replace
            fallback, _ = scope.view.predict(replace(query, command=None))
            models['tool_class'] = fallback.model_dump(mode='json')
            from clawtune_sidecar.contracts.load_prediction import summarize_pmu_evidence
            from clawtune_sidecar.predictors.tool_resource import _resource_class_for_duration_ms
            pmu = summarize_pmu_evidence(scope.view.tool.predict_pmu_samples(query))
            selected = self.metadata(payload['repository'], models, payload['command'], payload['command_sha256'])
            receipt = secrets.token_urlsafe(32)
            revision = digest({'seed': self.seed.digest, 'mapping': self.policy_digest,
                               'read_set': [(digest(scope.identity), scope.view.generation)]})
            prediction = dict(tool=models['tool'], trie=models['trie'], lattice=models['lattice'],
                call_prediction=models['tool'], diagnostics=diagnostics.model_dump(mode='json'),
                duration_p50_ms=None if tool.targets['duration_ms'].p50 is None else round(tool.targets['duration_ms'].p50),
                duration_p90_ms=None if tool.targets['duration_ms'].p90 is None else round(tool.targets['duration_ms'].p90),
                resource_class=_resource_class_for_duration_ms(tool.targets['duration_ms'].p90),
                confidence=None, pmu_prediction=pmu.model_dump(mode='json'), tool_resource=None)
            response = dict(schema_version=PROTOCOL, seed_sha256=self.seed.digest,
                mapping_policy_sha256=self.policy_digest, scope_generation=scope.view.generation,
                view_revision=revision, prediction_receipt=receipt, prediction=prediction,
                admission_prediction={k: v for k, v in selected.items() if k != 'models'})
            validate_message(response, 'prediction')
            item = dict(request_digest=request_digest, response=response, selected=selected,
                        command_sha256=payload['command_sha256'], command=payload['command'],
                        tool_name=payload['tool_name'], call_key=key, execution_id=None,
                        bindings=scope.features._calls.get(key, {}))
            scope.predictions[key] = item
            scope.receipts[hashlib.sha256(receipt.encode()).hexdigest()] = item
            return response

    @_measured
    def claim(self, session_id, request):
        scope = self.scopes[session_id]
        receipt_hash = hashlib.sha256(str(request.get('prediction_receipt', '')).encode()).hexdigest()
        execution_id = request['execution_id']
        with scope.lock:
            existing = scope.claims.get(execution_id)
            if existing is not None:
                if existing['receipt_hash'] != receipt_hash or existing['command_sha256'] != request['command_sha256']:
                    raise ValueError('conflicting execution claim')
                return dict(existing['selected'])
            item = scope.receipts.get(receipt_hash)
            if item is None or item['command_sha256'] != request['command_sha256'] or item['command'] != request['command']:
                raise ValueError('missing or mismatched host prediction receipt')
            if item['execution_id'] not in (None, execution_id):
                raise ValueError('prediction receipt already claimed')
            row = dict(kind='claim', execution_id=execution_id, receipt_hash=receipt_hash,
                command=item['command'], command_sha256=item['command_sha256'], tool_name=item['tool_name'],
                call_key=item['call_key'], selected=item['selected'], bindings=item['bindings'])
            self._append(scope, row)
            scope.claims[execution_id] = row
            item['execution_id'] = execution_id
            return dict(item['selected'])

    @_measured
    def observe(self, session_id, payload):
        validate_message(payload, 'observe')
        scope = self._scope(session_id, payload)
        execution_id = payload['execution_id']
        with scope.lock:
            claim = scope.claims.get(execution_id)
            receipt_hash = hashlib.sha256(payload['prediction_receipt'].encode()).hexdigest()
            if claim is None or any(claim[k] != payload[k] for k in ('command', 'command_sha256', 'tool_name')) or claim['receipt_hash'] != receipt_hash:
                raise ValueError('observation does not match claimed admission')
            call, clauses = learning_records(payload)
            if call is not None:
                if call.repo != payload['repository'] or call.command != payload['command'] or call.tool_name != payload['tool_name']:
                    raise ValueError('call learning identity mismatch')
                if training_target_provenance(call) != payload['call_provenance']:
                    raise ValueError('call target provenance mismatch')
            if any(row.repo != payload['repository'] for row in clauses):
                raise ValueError('clause learning repository mismatch')
            # Rebind the exact prediction-time features, including after restart.
            scope.features._calls[claim['call_key']] = {
                int(k): (tuple(v[0]), v[1]) for k, v in claim['bindings'].items()}
            clauses = tuple(scope.features.bind_observations(clauses, claim['call_key'], release=True))
            from dataclasses import asdict
            normalized = {**payload, 'clauses': [asdict(row) for row in clauses]}
            payload_digest = digest(normalized)
            previous = scope.committed.get(execution_id)
            if previous is not None:
                if previous[0] != payload_digest:
                    raise ValueError('execution reused with different learning payload')
                return dict(status='duplicate', scope_generation=previous[1], payload_sha256=payload_digest)
            prepare_started = time.monotonic()
            successor = scope.view.successor(call, clauses)
            self.metrics.append(dict(operation="prepare_overlay", session_id=session_id,
                                     status="ok", seconds=time.monotonic() - prepare_started))
            # Receipt is a session-local opaque handle, not authentication. The
            # durable payload stores its hash instead of the reusable wire value.
            durable = dict(normalized, prediction_receipt=receipt_hash)
            self._append(scope, dict(kind='observe', payload=durable, payload_digest=digest(durable),
                                     wire_digest=payload_digest, generation=successor.generation))
            scope.view = successor
            scope.committed[execution_id] = (payload_digest, successor.generation)
            return dict(status='accepted', scope_generation=successor.generation, payload_sha256=payload_digest)

    def mark_degraded(self, session_id, execution_id, reason):
        scope = self.scopes[session_id]
        with scope.lock:
            self._append(scope, dict(kind='degraded', execution_id=execution_id, reason=str(reason)[:1024]))
            scope.degraded = True

    def process_sample(self):
        import psutil
        process = psutil.Process()
        cpu = process.cpu_times()
        return dict(measurement='whole_worker_process_including_host_kb',
                    rss_bytes=process.memory_info().rss, cpu_seconds=cpu.user + cpu.system,
                    threads=process.num_threads(), active_scopes=len(self.scopes))

    def summary(self):
        return {**self.identity, 'seed_file_bytes': sum(p.stat().st_size for p in self.seed.path.glob('*.json')),
                'journal_bytes': sum((s.path / 'journal.jsonl').stat().st_size
                                     for s in self.scopes.values() if (s.path / 'journal.jsonl').exists()),
                'scopes': {sid: {'generation': s.view.generation,
            'learning_valid': not s.degraded and len(s.committed) == len(s.claims),
            'uncommitted_execution_ids': sorted(set(s.claims) - set(s.committed)),
            'claimed_executions': len(s.claims),
            'committed_executions': len(s.committed)} for sid, s in self.scopes.items()}}
