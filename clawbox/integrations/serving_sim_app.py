"""Management routes around an unmodified serving_sim ASGI application.

The upstream service owns its engine, clock, request handling and original
routes. These ClawBox routes only expose run identity and aggregate its feed.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import time


INFO_PATH = "/v1/clawbox/simulator/info"
EVENTS_PATH = "/request-signals/clawbox/events"
ADAPTER_PROTOCOL = 2
OUTCOMES_PATH = '/v1/clawbox/simulator/tool-outcomes'


def attach_persistence(app, out: Path, mode: str):
    """Optionally defer native cumulative reports until the driver has stopped.

    Native save() computes reporting statistics and rewrites all recorded inputs;
    it does not advance the simulation or flush an execution queue.
    """
    if mode not in {'native', 'final'}:
        raise ValueError('persistence must be native or final')
    app.state.persistence = mode
    upstream = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        service = original_save = None
        deferred = 0
        closed = False
        def defer_save():
            nonlocal deferred
            deferred += 1
        marker = out / 'clawbox-persistence.json'
        try:
            async with upstream(application):
                service = application.state.service
                original_save = service.save
                marker.write_text(json.dumps(dict(mode=mode, finalized=False)) + '\n')
                if mode == 'final':
                    service.save = defer_save
                yield
            closed = True
        finally:
            if original_save is not None:
                service.save = original_save
                # The native lifespan has awaited its paced driver and released
                # its renderer. All request handlers have drained before this.
                started = time.monotonic()
                original_save()
                marker.write_text(json.dumps(dict(mode=mode, finalized=True,
                    native_close_succeeded=closed, deferred_saves=deferred,
                    final_save_seconds=time.monotonic() - started)) + '\n')

    app.router.lifespan_context = lifespan
    return app


def attach_chat_adapter(app):
    """Add current-request identity metadata without changing native scheduling.

    The catalog is shared by all sessions. Only background-handle observations
    are indexed; CPU clients never receive recorded conversations or future calls.
    """
    from fastapi import Request, HTTPException
    from clawbox.replay.process_sessions import bind_process_sessions, _RUNNING
    from serving_sim.workload.trace_reference import ReplayError

    route = next(r for r in app.routes if r.path == '/v1/chat/completions')
    upstream = route.endpoint
    expected_handles, sessions = {}, {}

    async def completions(request):
        body = await request.json()  # Starlette caches this for the native route.
        if not isinstance(body, dict):
            return await upstream(request)
        service = app.state.service
        try:
            binding = service.registry.prepare(body)
        except ReplayError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        ref = binding.ref
        key = ref.trace_id, ref.call_id
        if key not in expected_handles:
            expected_handles[key] = [m for m in ref.messages if isinstance(m, dict)
                and m.get('role') == 'tool' and isinstance(m.get('content'), str)
                and _RUNNING.search(m['content'])]
        previous = sessions.get(binding.session_id, {})
        try:
            mapped = (bind_process_sessions(expected_handles[key], body.get('messages'), previous)
                      if expected_handles[key] else previous)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        response = await upstream(request)
        sessions[binding.session_id] = mapped
        native_stream = response.body_iterator

        async def stream():
            try:
                first = True
                async for chunk in native_stream:
                    if first:
                        metadata = 'event: clawbox\ndata: ' + json.dumps(dict(
                            protocol=ADAPTER_PROTOCOL, **binding.metadata(), process_sessions=mapped)) + '\n\n'
                        chunk = metadata + chunk if isinstance(chunk, str) else metadata.encode() + chunk
                        first = False
                    yield chunk
            finally:
                await native_stream.aclose()
        response.body_iterator = stream()
        return response

    completions.__annotations__['request'] = Request
    app.routes.remove(route)
    app.add_api_route('/v1/chat/completions', completions, methods=['POST'])
    return app


def catalog_digest(catalog: Path) -> str:
    # Native startup validates and loads trajectories once. Identity checking
    # needs only bytes, not a second copy of every recorded conversation.
    catalog = catalog.expanduser().resolve()
    identities = []
    for spec in json.loads(catalog.read_text())['traces']:
        path = (catalog.parent / Path(spec['path']).expanduser()).resolve()
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        identities.append(dict(trace_id=spec['trace_id'], agent_id=spec.get('agent_id'), sha256=digest))
    return hashlib.sha256(json.dumps(sorted(identities, key=lambda s: s['trace_id']),
                                     sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def attach_management(app, *, input_digest: str):
    from fastapi import Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from serving_sim.signals.signal_delivery import CursorError, SignalHTTPError

    async def tool_outcomes(request):
        from fastapi import HTTPException
        from types import SimpleNamespace
        from clawbox.replay.tool_outcomes import match_tool_failures
        body = await request.json()
        service = app.state.service
        sid, tid = body.get('session_id'), body.get('trace_id')
        session = service.registry.sessions.get(sid)
        if session is None or session.trace_id != tid:
            raise HTTPException(409, 'tool validation session/trace mismatch')
        # End-of-session audit only; no extra request on model or Tool hot paths.
        actions = [SimpleNamespace(input=r.messages, output=r.message) for r in service.refs.traces[tid]]
        def classify():
            harmless, unexpected = [], []
            matched = match_tool_failures(actions, body['spans'], session_id=sid,
                reported_failures=body['reported_failures'], harmless_failures=harmless,
                unexpected_failures=unexpected)
            return dict(session_id=sid, trace_id=tid, matched=matched,
                        harmless=harmless, unexpected=unexpected)
        return await asyncio.to_thread(classify)

    tool_outcomes.__annotations__['request'] = Request
    app.add_api_route(OUTCOMES_PATH, tool_outcomes, methods=['POST'])

    @app.get(INFO_PATH)
    async def info():
        service = getattr(app.state, 'service', None)
        if service is None:
            return JSONResponse(dict(ready=False, error='simulator initialization is incomplete'), status_code=503)
        topology = json.dumps(service.cfg['topology'], sort_keys=True, separators=(',', ':'))
        result = dict(schema_version=1, adapter='clawbox', adapter_protocol=ADAPTER_PROTOCOL,
                      service_id=service.feed.epoch,
                      ready=service.running and not service.driver.done(),
                      accepted_requests=len(service.registry.requests), server_wall_time=time.time(),
                      clock=dict(v0=service.clock.v0, k=service.clock.k, wall_epoch0=service.epoch0),
                      catalog_digest=input_digest, trace_ids=sorted(service.refs.traces),
                      traces={tid: dict(model_calls=len(calls),
                          missing_reasoning_calls=sum(r.reasoning_tokens is None for r in calls))
                          for tid, calls in service.refs.traces.items()},
                      topology_digest=hashlib.sha256(topology.encode()).hexdigest(),
                      signals=dict(**service.feed.head(), service_events_path=EVENTS_PATH),
                      cpu_affinity=sorted(os.sched_getaffinity(0)),
                      persistence=getattr(app.state, 'persistence', 'native'))
        return JSONResponse(result, status_code=200 if result['ready'] else 503)

    async def events(request, after: str | None = None):
        feed = app.state.feed
        try:
            start = feed.parse_cursor(after)
        except CursorError as exc:
            raise SignalHTTPError(exc.status_code, dict(error=str(exc), **feed.head())) from exc

        async def stream():
            cursor = start
            while not await request.is_disconnected():
                try:
                    rows = feed.matching(cursor, None, None)
                    head = feed.sequence
                except CursorError as exc:
                    yield 'event: gap\ndata: ' + json.dumps(dict(error=str(exc), **feed.head())) + '\n\n'
                    return
                feed.changed.clear()
                for sequence, record in rows:
                    yield f'id: {feed.cursor(sequence)}\nevent: signal\ndata: ' + json.dumps(record, allow_nan=False) + '\n\n'
                    if feed.delivery is not None:
                        feed.delivery(record)
                cursor = head
                if not rows:
                    yield ': keepalive\n\n'
                try:
                    await asyncio.wait_for(feed.changed.wait(), .01)
                except TimeoutError:
                    pass
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-store'})

    events.__annotations__['request'] = Request
    app.add_api_route(EVENTS_PATH, events, methods=['GET'])
    return app


def create_app(out: Path, topology, trace_catalog: Path, *, persistence='native', **options):
    from serving_sim.joined.joined import create_app as upstream_app
    digest = catalog_digest(trace_catalog)
    app = upstream_app(out, topology, trace_catalog, **options)
    attach_persistence(app, out, persistence)
    return attach_management(attach_chat_adapter(app), input_digest=digest)
