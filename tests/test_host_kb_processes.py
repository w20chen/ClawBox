from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
import time

import pytest

from clawbox.experiments.knowledge.processes import ProcessKnowledgeManager
from test_host_kb import setup_manager, prediction_payload
from tool_resource.runtime_kb import CompletedCall, training_target_provenance


def metadata(repo, models, command, digest):
    return {'models': models}


def manager_at(tmp_path, **kwargs):
    return ProcessKnowledgeManager(seed_path=tmp_path/'seed', root=tmp_path/'process-kb',
        run_id='run', attempt_id='attempt', arm_id='arm', clawtune_revision='test',
        metadata=metadata, processes=2, **kwargs)


def test_process_ownership_commit_retry_recovery_and_equivalence(tmp_path):
    reference = setup_manager(tmp_path)()
    manager = manager_at(tmp_path)
    pids = [owner.process.pid for owner in manager.owners]
    try:
        for session in ('a', 'b'):
            reference.reserve(session, 'repo')
            manager.reserve(session, 'repo')
        request = prediction_payload()
        predicted = manager.predict('a', request)
        assert predicted['prediction'] == reference.predict('a', request)['prediction']
        assert manager.predict('a', request) == predicted
        receipt = predicted['prediction_receipt']
        claim = dict(execution_id='exec', prediction_receipt=receipt,
                     command=request['command'], command_sha256=request['command_sha256'])
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: manager.claim('a', claim), range(8)))
        assert all(value == claims[0] for value in claims)
        call = CompletedCall('repo', 'exec', 'sleep 1', 1., 4.)
        observation = {k:v for k,v in request.items()
                       if k not in ('event_id', 'tool_call_id', 'command_context', 'raw_params')}
        observation.update(execution_id='exec', prediction_receipt=receipt, call=asdict(call), clauses=[],
            call_provenance=training_target_provenance(call),
            quality=dict(collection_validity='valid', cleanup='ok', loss_total=0, reason=None))
        with ThreadPoolExecutor(max_workers=8) as pool:
            acknowledgements = list(pool.map(lambda _: manager.observe('a', observation), range(8)))
        assert sum(v['status']=='accepted' for v in acknowledgements) == 1
        assert {v['scope_generation'] for v in acknowledgements} == {1}
        assert manager.scope_summary('b')['generation'] == 0
        assert manager.summary()['scopes']['a']['generation'] == 1
        assert any(v['operation']=='journal_wait' for v in manager.metrics)
    finally:
        manager.close()
    assert all(not owner.process.is_alive() for owner in manager.owners)
    restarted = manager_at(tmp_path)
    try:
        restarted.reserve('a', 'repo')
        assert restarted.observe('a', observation)['status'] == 'duplicate'
        with pytest.raises(ValueError, match='different learning payload'):
            restarted.observe('a', {**observation, 'call': {**observation['call'], 'ts_end': 5.}})
        assert restarted.scope_summary('a')['generation'] == 1
    finally:
        restarted.close()


def test_dead_owner_fails_pending_requests_without_hanging(tmp_path):
    setup_manager(tmp_path)
    manager = manager_at(tmp_path)
    try:
        manager.reserve('a', 'repo')
        owner = manager._owner('a')
        owner.process.terminate()
        owner.process.join(5)
        owner.reader.join(5)
        with pytest.raises(RuntimeError, match='owner exited'):
            manager.predict('a', prediction_payload())
    finally:
        manager.close()


@dataclass
class SlowJournalMetadata:
    marker: str

    def __call__(self, repo, models, command, digest):
        from clawbox.experiments.host_kb import HostKnowledgeManager
        original = HostKnowledgeManager._append
        if not getattr(original, 'test_slow', False):
            marker = Path(self.marker)
            def append(manager, scope, row):
                if scope.identity['session_id']=='a' and row['kind']=='claim':
                    marker.write_text('waiting')
                    deadline = time.monotonic()+10
                    while not marker.with_suffix('.release').exists():
                        if time.monotonic()>deadline:
                            raise TimeoutError('test journal was not released')
                        time.sleep(.01)
                return original(manager, scope, row)
            append.test_slow = True
            HostKnowledgeManager._append = append
        return metadata(repo, models, command, digest)


def test_journal_wait_blocks_own_pair_but_not_another_pair(tmp_path):
    setup_manager(tmp_path)
    marker = tmp_path/'journal'
    manager = ProcessKnowledgeManager(seed_path=tmp_path/'seed', root=tmp_path/'process-kb',
        run_id='run', attempt_id='attempt', arm_id='arm', clawtune_revision='test',
        metadata=SlowJournalMetadata(str(marker)), processes=1)
    try:
        for session in ('a','b'):
            manager.reserve(session,'repo')
        request = prediction_payload()
        receipt = manager.predict('a',request)['prediction_receipt']
        claim = manager.submit('claim','a',dict(execution_id='exec', prediction_receipt=receipt,
            command=request['command'],command_sha256=request['command_sha256']))
        deadline = time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:
            time.sleep(.01)
        assert marker.exists()
        same_pair = manager.submit('predict','a',{**request,'tool_call_id':'next'})
        other = manager.submit('predict','b',prediction_payload('b')).result(timeout=3)
        assert other['scope_generation']==0
        assert not same_pair.done() and not claim.done()
        marker.with_suffix('.release').touch()
        claim.result(timeout=3)
        same_pair.result(timeout=3)
    finally:
        marker.with_suffix('.release').touch()
        manager.close()
