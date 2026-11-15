"""Run with the unmodified simulator checkout on PYTHONPATH."""
import asyncio
import json
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi import FastAPI, HTTPException

sd = pytest.importorskip('serving_sim.signals.signal_delivery')
from clawbox.integrations.serving_sim_app import attach_management, INFO_PATH, EVENTS_PATH


def test_signal_batch_keeps_order_and_gap_checks_without_rescanning_old_history():
    from collections import deque
    from clawbox.integrations.serving_sim_app import signal_batch
    class Counted(deque):
        visited = 0
        def __reversed__(self):
            for item in super().__reversed__():
                self.visited += 1
                yield item
    feed = sd.Feed()
    feed.sequence = 100010
    feed.events = Counted((i, dict(request_id=str(i))) for i in range(11, 100011))
    expected = feed.matching(100002, None, None)
    assert signal_batch(feed, 100002) == expected
    assert feed.events.visited == 9
    assert signal_batch(feed, feed.sequence) == []
    with pytest.raises(sd.CursorError, match='expired'):
        signal_batch(feed, 9)
    with pytest.raises(sd.CursorError, match='between'):
        signal_batch(feed, feed.sequence + 1)


@pytest.mark.parametrize('mode', ['native', 'final'])
@pytest.mark.parametrize('close_fails', [False, True])
def test_persistence_drains_driver_before_saving_all_records(tmp_path, mode, close_fails):
    from contextlib import asynccontextmanager
    from clawbox.integrations.serving_sim_app import attach_persistence
    records, writes = [], []
    service = NS(running=True)
    files_closed = False
    def save(final=False):
        assert not files_closed, 'must not save after native close has closed inputs'
        writes.append((final, service.running, list(records)))
    service.save = save
    @asynccontextmanager
    async def native_lifespan(application):
        nonlocal files_closed
        application.state.service = service
        service.save(final=True)
        try:
            yield
        finally:
            # Model a final driver event during its awaited shutdown.
            records.append('driver-stopped')
            service.running = False
            if close_fails:
                raise RuntimeError('driver failed')
            service.save(final=True)
            files_closed = True
    application = attach_persistence(FastAPI(lifespan=native_lifespan), tmp_path, mode)
    async def run():
        async with application.router.lifespan_context(application):
            for index in range(1000):
                records.append(index)
                service.save()
            assert not json.loads((tmp_path/'clawbox-persistence.json').read_text())['finalized']
    if close_fails:
        with pytest.raises(RuntimeError, match='driver failed'):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert service.save is save
    assert len(writes) == (1001 if close_fails else 1002)
    assert sum(not row[0] for row in writes) == 1000  # native flushing/step release is never suppressed
    if not close_fails:
        assert writes[-1] == (True, False, records)
    marker = json.loads((tmp_path/'clawbox-persistence.json').read_text())
    assert marker['finalized'] == marker['native_close_succeeded'] == (not close_fails)
    assert marker['report_policy'] == 'native_final'


def test_catalog_identity_hashes_bytes_without_loading_conversations(tmp_path):
    import hashlib
    from clawbox.integrations.serving_sim_app import catalog_digest
    from clawbox.experiments.serving_sim_inputs import catalog_digest as expected_digest
    # A digest pass must not parse trajectory JSON; the native loader owns that.
    data = b'large-recorded-conversation' * 10000
    (tmp_path / 'trace.jsonl').write_bytes(data)
    (tmp_path / 'catalog.json').write_text(json.dumps(dict(traces=[
        dict(trace_id='trace', agent_id='agent', path='trace.jsonl')])) )
    assert catalog_digest(tmp_path / 'catalog.json') == expected_digest([
        dict(trace_id='trace', agent_id='agent', sha256=hashlib.sha256(data).hexdigest())])


def test_gpu_adapter_process_mapping_is_per_session_and_keeps_native_output():
    from fastapi import Request
    from fastapi.responses import StreamingResponse
    from serving_sim.workload.trace_reference import RecordedCall, ReplayRegistry
    from clawbox.integrations.serving_sim_app import attach_chat_adapter
    recorded_messages = [dict(role='tool', tool_call_id='background-call',
                              content='Command still running (session recorded, pid 10)')]
    ref = RecordedCall('trace', 0, 'call', recorded_messages,
                       dict(role='assistant', content='recorded'), 2, None, 'stop')
    catalog = NS(traces={'trace': (ref,)}, get=lambda tid, index: ref)
    application = FastAPI()
    sd.attach_routes(application)
    registry = ReplayRegistry(catalog)
    application.state.service = NS(registry=registry)
    diagnostics = []
    application.state.diagnostics = NS(write=lambda event, **fields: diagnostics.append((event, fields)))
    closed = []
    async def native(request):
        body = request.state.request_signal_body
        registry.accept(registry.prepare(body))
        async def stream():
            try:
                yield 'data: {"native":"recorded"}\n\n'
                yield 'data: [DONE]\n\n'
            finally:
                closed.append(body['request_id'])
        return StreamingResponse(stream(), media_type='text/event-stream')
    native.__annotations__['request'] = Request
    application.add_api_route('/v1/chat/completions', native, methods=['POST'])
    attach_chat_adapter(application)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://test') as client:
            for sid, handle, pid in [('a', 'live-a', 101), ('b', 'live-b', 202)]:
                body = dict(session_id=sid, request_id=sid, replay=dict(trace_id='trace', call_id=0),
                    messages=[dict(role='tool', tool_call_id='background-call',
                        content=f'Command still running (session {handle}, pid {pid})')])
                response = await client.post('/v1/chat/completions', json=body)
                assert response.status_code == 200
                header = json.loads(response.text.split('\n')[1][6:])
                assert header['process_sessions']['recorded']['session_id'] == handle
                assert header['process_sessions']['recorded']['pid'] == str(pid)
                assert 'data: {"native":"recorded"}' in response.text
                assert (await client.post('/v1/chat/completions', json=body)).status_code == 409
            body.update(request_id='b-next')
            body['messages'][0]['content'] = 'Command still running (session changed, pid 303)'
            assert (await client.post('/v1/chat/completions', json=body)).status_code == 409
        assert set(closed) == {'a', 'b'}
        assert len(registry.requests) == 2
        assert ref.messages == recorded_messages
        for event in ('first_sse_emitted', 'done_emitted', 'stream_closed'):
            assert {fields['request_id'] for kind, fields in diagnostics if kind == event} == {'a', 'b'}
    asyncio.run(run())


def app():
    result = FastAPI()
    feed = sd.attach_routes(result, ['key'])
    result.state.feed = feed
    result.state.service = NS(feed=feed, cfg={'topology': {'cards': []}},
        running=True, driver=NS(done=lambda: False), registry=NS(requests={}),
        refs=NS(traces={'trace': ()}), clock=NS(v0=0., k=1.), epoch0=100., trace_prompts=None)
    return attach_management(result, input_digest='catalog')


def test_adapter_auth_readiness_and_original_routes(monkeypatch):
    monkeypatch.setattr('os.sched_getaffinity', lambda pid: {240, 241}, raising=False)
    async def run():
        application = app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://test') as client:
            assert (await client.get(INFO_PATH)).status_code == 401
            assert (await client.get(EVENTS_PATH)).status_code == 401
            headers = {'Authorization': 'Bearer key'}
            response = await client.get(INFO_PATH, headers=headers)
            assert response.status_code == 200
            data = response.json()
            assert data['adapter'] == 'clawbox' and data['accepted_requests'] == 0
            assert data['catalog_digest'] == 'catalog' and data['clock']['k'] == 1
            assert data['prompt_source'] == 'actual'
            assert data['service_id'] == data['signals']['epoch']
            # Upstream's original endpoint still requires its original filter.
            assert (await client.get('/request-signals/events', headers=headers)).status_code == 422
            assert (await client.get('/v1/simulator/info', headers=headers)).status_code == 404
            application.state.service.driver = NS(done=lambda: True)
            assert (await client.get(INFO_PATH, headers=headers)).status_code == 503
    asyncio.run(run())


def test_adapter_all_sessions_cursor_concurrent_publish_and_epoch():
    class Request:
        async def is_disconnected(self): return False
    async def run():
        application = app()
        feed = application.state.feed
        endpoint = next(r.endpoint for r in application.routes if r.path == EVENTS_PATH)
        def publish(n):
            feed.publish(dict(engine_id='A', request_id=f'r{n}', session_id=f's{n % 2}', event='queued'))
        publish(1); publish(2)
        stream = (await endpoint(Request(), after='0')).body_iterator
        first = await stream.__anext__()
        publish(3)  # publish while the adapter is suspended at yield
        second, third = await stream.__anext__(), await stream.__anext__()
        assert [json.loads(x.split('data: ')[1])['request_id'] for x in [first, second, third]] == ['r1', 'r2', 'r3']
        await stream.aclose()
        with pytest.raises(HTTPException) as error:
            await endpoint(Request(), after='different-epoch:3')
        assert error.value.status_code == 409
    asyncio.run(run())


def test_metadata_reuses_valid_cache_and_rejects_wrong_config(monkeypatch, tmp_path):
    import sys
    from clawbox.integrations.serving_sim import metadata
    fetched = []
    monkeypatch.setitem(sys.modules, 'serving_sim.setup', NS(MODELS={'cached':'pin1','missing':'pin2'}, METADATA=['config.json']))
    def resolve(model, revision, directory=None):
        if model == 'missing' and not fetched: raise FileNotFoundError('missing')
        return tmp_path/model
    def download(model, **kwargs): fetched.append(model); return str(tmp_path/model)
    monkeypatch.setitem(sys.modules, 'serving_sim.joined.joined', NS(local_tokenizer_directory=resolve))
    monkeypatch.setitem(sys.modules, 'huggingface_hub', NS(snapshot_download=download))
    metadata()
    assert fetched == ['missing']
    def reject(*args): raise ValueError('wrong pinned config')
    monkeypatch.setitem(sys.modules, 'serving_sim.joined.joined', NS(local_tokenizer_directory=reject))
    with pytest.raises(ValueError, match='wrong pinned config'): metadata()
    assert fetched == ['missing']
