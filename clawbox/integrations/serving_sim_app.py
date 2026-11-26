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

from clawbox.experiments.serving_sim_diagnostics import Diagnostics, exception_record


INFO_PATH = "/v1/clawbox/simulator/info"
EVENTS_PATH = "/request-signals/clawbox/events"
ADAPTER_PROTOCOL = 3
OUTCOMES_PATH = '/v1/clawbox/simulator/tool-outcomes'
CPU_FEEDBACK_PATH = '/v1/clawbox/simulator/cpu-feedback'


def attach_diagnostics(app, out: Path, interval_s: float):
    upstream = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with upstream(application):
            diagnostics = Diagnostics(out / 'clawbox-diagnostics.jsonl')
            application.state.diagnostics = diagnostics
            sampler = asyncio.create_task(diagnostics.sample_loop(interval_s))
            try:
                yield
            finally:
                sampler.cancel()
                await asyncio.gather(sampler, return_exceptions=True)
                diagnostics.close()
    app.router.lifespan_context = lifespan
    return app


def diagnose(app, event, **fields):
    diagnostics = getattr(app.state, 'diagnostics', None)
    if diagnostics is not None:
        diagnostics.write(event, **fields)


def attach_persistence(app, out: Path, mode: str):
    """Record native finalization without suppressing periodic input/step cleanup.

    Current sim-release saves summaries only at startup and close. Its ordinary
    save() flushes incremental inputs and releases obsolete step rows. Both
    persistence labels therefore use that native lifecycle unchanged.
    """
    if mode not in {'native', 'final'}:
        raise ValueError('persistence must be native or final')
    app.state.persistence = mode
    upstream = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        closed = False
        started = None
        marker = out / 'clawbox-persistence.json'
        try:
            async with upstream(application):
                marker.write_text(json.dumps(dict(mode=mode, finalized=False)) + '\n')
                try:
                    yield
                finally:
                    started = time.monotonic()
            closed = True
        finally:
            if started is not None:
                # Native close flushes decode spans, saves with final=True and
                # closes incremental files. Calling save again would use closed files.
                marker.write_text(json.dumps(dict(mode=mode, finalized=closed,
                    native_close_succeeded=closed, report_policy='native_final',
                    native_close_seconds=time.monotonic() - started)) + '\n')

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
        # Reuse the JSON already validated by the native ASGI middleware.
        body = request.state.request_signal_body
        if not isinstance(body, dict):
            return await upstream(request)
        identity = {key: body.get(key) for key in ('session_id', 'request_id')}
        diagnose(app, 'request_received', **identity)
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
        try:
            response = await upstream(request)
        except BaseException as exc:
            diagnose(app, 'request_rejected', **identity, error=exception_record(exc))
            raise
        diagnose(app, 'response_started', **identity, http_status=response.status_code)
        sessions[binding.session_id] = mapped
        native_stream = response.body_iterator

        async def stream():
            exhausted = False
            try:
                first = True
                async for chunk in native_stream:
                    if chunk == 'data: [DONE]\n\n' or chunk == b'data: [DONE]\n\n':
                        diagnose(app, 'done_emitted', **identity)
                    if first:
                        diagnose(app, 'first_sse_emitted', **identity)
                        metadata = 'event: clawbox\ndata: ' + json.dumps(dict(
                            protocol=ADAPTER_PROTOCOL, **binding.metadata(), process_sessions=mapped)) + '\n\n'
                        chunk = metadata + chunk if isinstance(chunk, str) else metadata.encode() + chunk
                        first = False
                    yield chunk
                exhausted = True
            except BaseException as exc:
                diagnose(app, 'stream_error', **identity, error=exception_record(exc))
                raise
            finally:
                try:
                    await native_stream.aclose()
                finally:
                    diagnose(app, 'stream_closed', **identity, exhausted=exhausted)
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


def signal_batch(feed, after: int) -> list:
    """Read the unfiltered tail without rescanning already delivered history.

    The native bounded deque and cursor contract stay unchanged. This function
    takes one synchronous snapshot, before any SSE yield can publish more events.
    """
    from serving_sim.signals.signal_delivery import CursorError
    if after < 0 or after > feed.sequence:
        raise CursorError(400, 'signal cursor must be between zero and the head')
    if feed.events and after < feed.events[0][0] - 1:
        raise CursorError(409, 'signal cursor expired; resume from the reported head')
    rows = []
    for sequence, record in reversed(feed.events):
        if sequence <= after:
            break
        rows.append((sequence, record))
    rows.reverse()
    return rows


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
        nofile = None
        if os.name == 'posix':
            import resource
            nofile = list(resource.getrlimit(resource.RLIMIT_NOFILE))
        service = getattr(app.state, 'service', None)
        if service is None:
            return JSONResponse(dict(ready=False, error='simulator initialization is incomplete'), status_code=503)
        topology = json.dumps(service.cfg['topology'], sort_keys=True, separators=(',', ':'))
        result = dict(schema_version=1, adapter='clawbox', adapter_protocol=ADAPTER_PROTOCOL,
                      service_id=service.feed.epoch,
                      ready=service.running and not service.driver.done(),
                      accepted_requests=len(service.registry.requests), server_wall_time=time.time(),
                      clock=dict(v0=service.clock.v0, k=service.clock.k, wall_epoch0=service.epoch0),
                      prompt_source='actual' if service.trace_prompts is None else 'trace',
                      catalog_digest=input_digest, trace_ids=sorted(service.refs.traces),
                      traces={tid: dict(model_calls=len(calls),
                          missing_reasoning_calls=sum(r.reasoning_tokens is None for r in calls))
                          for tid, calls in service.refs.traces.items()},
                      topology_digest=hashlib.sha256(topology.encode()).hexdigest(),
                      decode_time={key: service.cfg[key] for key in ('decode_time_scale', 'tpot_ms')
                                   if service.cfg.get(key) is not None},
                      cpu_refill=service.cpu_refill.stats(),
                      response_forecast=service.clawbox_forecast,
                      non_physical=service.cfg.get('validity', {}).get('non_physical', {}),
                      signals=dict(**service.feed.head(), service_events_path=EVENTS_PATH),
                      cpu_affinity=sorted(os.sched_getaffinity(0)),
                      pid=os.getpid(), nofile=nofile,
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
                    rows = signal_batch(feed, cursor)
                    head = feed.sequence
                except CursorError as exc:
                    yield 'event: gap\ndata: ' + json.dumps(dict(error=str(exc), **feed.head())) + '\n\n'
                    return
                feed.changed.clear()
                for sequence, record in rows:
                    delivered = dict(record, adapter_emit_unix_s=time.time())
                    yield f'id: {feed.cursor(sequence)}\nevent: signal\ndata: ' + json.dumps(delivered, allow_nan=False) + '\n\n'
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


def create_app(out: Path, topology, trace_catalog: Path, *, persistence='native',
               diagnostic_interval_seconds=1., forecast_interval_seconds=.5,
               forecast_window_seconds=5., forecast_validity_seconds=2., **options):
    from serving_sim.joined.joined import create_app as upstream_app
    digest = catalog_digest(trace_catalog)
    app = upstream_app(out, topology, trace_catalog, prompt_source='actual', **options)
    from .response_forecast import attach_response_forecasts
    attach_response_forecasts(app, interval=forecast_interval_seconds,
                              window=forecast_window_seconds, validity=forecast_validity_seconds)
    attach_persistence(app, out, persistence)
    attach_diagnostics(app, out, diagnostic_interval_seconds)
    return attach_management(attach_chat_adapter(app), input_digest=digest)
