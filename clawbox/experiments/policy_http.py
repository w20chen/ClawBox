"""Async transport for the existing policy protocol, with separate callback lanes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import sys
import time

import uvicorn

from .policy_callback_pool import CallbackPool


def policy_event_loop(setting):
    """Select the configured loop without silently weakening a Linux run."""
    if setting not in {'auto', 'asyncio', 'uvloop'}:
        raise ValueError('policy_http_loop must be auto, asyncio or uvloop')
    selected = ('uvloop' if sys.platform == 'linux' else 'asyncio') if setting == 'auto' else setting
    if selected == 'asyncio':
        return selected, asyncio.new_event_loop
    try:
        import uvloop
    except ImportError as exc:
        raise RuntimeError(
            'execution.policy_http_loop requires uvloop; install the updated host dependencies '
            'with python -m pip install -e . from ClawBox, or explicitly select asyncio '
            'for a diagnostic run'
        ) from exc
    return selected, uvloop.new_event_loop


class PolicyHTTPServer:
    request_queue_size = 4096

    def __init__(self, address, owner, *, admission_workers, forecast_workers=16, event_loop='auto'):
        self.event_loop, self.loop_factory = policy_event_loop(event_loop)
        self.owner = owner
        self.failure = None
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(address)
        self.socket.listen(self.request_queue_size)
        self.socket.setblocking(False)
        self.server_address = self.socket.getsockname()
        self.admissions = ThreadPoolExecutor(max_workers=admission_workers, thread_name_prefix='policy-admit')
        self.boundaries = CallbackPool(64, 'policy-boundary')
        self.admission_dispatch = CallbackPool(1, 'policy-admission-dispatch')
        # Forecasts are optional observations, not command boundaries. Never
        # queue them behind one another after their caller/estimate has expired.
        self.forecasts = CallbackPool(forecast_workers, 'policy-forecast')
        self.forecast_workers = forecast_workers
        self._forecast_pending = set()
        self.server = uvicorn.Server(uvicorn.Config(self, host=address[0], port=address[1],
            loop=self.event_loop, http='h11', lifespan='off', access_log=False, log_level='critical',
            timeout_keep_alive=5, timeout_graceful_shutdown=5, backlog=self.request_queue_size))

    def serve_forever(self):
        try:
            with asyncio.Runner(loop_factory=self.loop_factory) as runner:
                runner.run(self.server.serve(sockets=[self.socket]))
        except BaseException as exc:
            self.failure = exc

    def wait_ready(self):
        deadline = time.monotonic()+10
        while not self.server.started:
            if self.failure is not None:
                raise RuntimeError(f'policy listener failed to start: {self.failure}')
            if time.monotonic() >= deadline:
                self.shutdown()
                raise RuntimeError('policy listener did not become ready within 10 seconds')
            time.sleep(.01)

    def shutdown(self):
        self.server.should_exit = True

    def server_close(self):
        self.socket.close()
        self.admission_dispatch.shutdown(wait=False, cancel_futures=True)
        self.admissions.shutdown(wait=False, cancel_futures=True)
        self.boundaries.shutdown(wait=False, cancel_futures=True)
        self.forecasts.shutdown(wait=False, cancel_futures=True)

    async def _callback(self, function, *args, admission=False, record=None):
        submitted = time.monotonic()

        def run():
            started = time.monotonic()
            try:
                return function(*args)
            finally:
                if record is not None:
                    record['callback_queue_seconds'] = record.get('callback_queue_seconds', 0.) + started-submitted
                    record['callback_seconds'] = record.get('callback_seconds', 0.) + time.monotonic()-started

        loop = asyncio.get_running_loop()
        if admission:
            # Growing the admission pool may create threads while holding
            # ThreadPoolExecutor's global lock. Do that away from HTTP intake.
            future = await loop.run_in_executor(self.admission_dispatch, self.admissions.submit, run)
            return await asyncio.wrap_future(future)
        return await loop.run_in_executor(self.boundaries, run)

    async def _forecast(self, session_id, receipts, disconnected, record):
        unavailable = {'tools': {receipt: {'phase': 'unavailable', 'remaining_ms': None,
            'wait_remaining_ms': None, 'sample_count': 0, 'reason': 'forecast_capacity_busy'}
            for receipt in receipts}, 'valid_for_ms': 2000}
        self._forecast_pending = {f for f in self._forecast_pending if not f.done()}
        if len(self._forecast_pending) >= self.forecast_workers or disconnected.is_set():
            record['forecast_capacity_busy'] = True
            return unavailable
        submitted = time.monotonic()

        def run():
            started = time.monotonic()
            try:
                if disconnected.is_set():
                    return unavailable
                return self.owner.forecaster.snapshot(session_id, receipts)
            finally:
                record['callback_queue_seconds'] = started-submitted
                record['callback_seconds'] = time.monotonic()-started

        future = self.forecasts.submit(run)
        self._forecast_pending.add(future)
        # Cancellation must not free a slot while the thread is still running.
        return await asyncio.shield(asyncio.wrap_future(future))

    @staticmethod
    def _validate_observation(state, body):
        # A session callback can hold this lock. Waiting on it in the ASGI
        # loop would stop acknowledgements for every other session as well.
        with state.condition:
            execution = state.executions.get(body.get('execution_id'))
            if execution is None or execution.admission is None:
                raise ValueError('observation has no admitted execution')

    async def _knowledge(self, operation, session, payload, record):
        manager = self.owner.knowledge_manager
        started = time.monotonic()
        try:
            if hasattr(manager, 'request'):
                return await manager.request(operation, session, payload)
            return await self._callback(getattr(manager, operation), session, payload)
        finally:
            record['knowledge_seconds'] = record.get('knowledge_seconds', 0.) + time.monotonic()-started

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        path, method = scope['path'], scope['method']
        owner = self.owner
        if method == 'GET' and path == '/v1/kb/health' and owner.knowledge_manager is not None:
            from .knowledge.protocol import PROTOCOL, MAX_BODY_BYTES
            await self._json(send, 200, dict(protocol_versions=[PROTOCOL], max_body_bytes=MAX_BODY_BYTES))
            return
        routes = {'/v1/runtime/session', '/v1/runtime/completion', '/v1/tool/admit', '/v1/tool/complete', '/v1/tool/start', '/v1/tool/forecast', '/v1/kb/predict', '/v1/kb/preview', '/v1/kb/observe'}
        if method != 'POST' or path not in routes:
            await self._json(send, 404, {'error': 'unknown control route'})
            return
        record = dict(path=path, request_start=time.monotonic(), request_wall_time_ns=str(time.time_ns()))
        state, body, watcher = None, None, None
        disconnected = asyncio.Event()
        admitted, duplicate = False, False
        try:
            headers = dict(scope['headers'])
            token = headers.get(b'authorization', b'').decode().removeprefix('Bearer ')
            state = owner._lookup(token)
            if state is None:
                record['status'] = 401
                await self._json(send, 401, {'error': 'unknown session capability'})
                return
            length = int(headers.get(b'content-length', b'0'))
            limit = 1048576 if path.startswith('/v1/kb/') else 65536
            if not 2 <= length <= limit:
                raise ValueError('invalid request size')
            chunks, size = [], 0
            while True:
                event = await asyncio.wait_for(receive(), timeout=5)
                if event['type'] == 'http.disconnect':
                    raise ConnectionResetError('client disconnected before request body')
                chunk = event.get('body', b'')
                size += len(chunk)
                if size > length:
                    raise ValueError('request body exceeds Content-Length')
                chunks.append(chunk)
                if not event.get('more_body'):
                    break
            if size != length:
                raise ValueError('incomplete request body')
            body = json.loads(b''.join(chunks))
            record['body_received_seconds'] = time.monotonic() - record['request_start']

            async def watch_disconnect():
                while True:
                    if (await receive())['type'] == 'http.disconnect':
                        disconnected.set()
                        return
            watcher = asyncio.create_task(watch_disconnect())
            if path == '/v1/runtime/completion':
                response = await self._callback(state.notify_runtime_completion, body, record=record)
                record.update(status=200, session_id=state.session_id, phase=body.get('phase'))
            elif path == '/v1/runtime/session':
                key = body.get('session_key')
                if not isinstance(key, str) or not 1 <= len(key) <= 512:
                    raise ValueError('invalid logical session key')
                response = await self._callback(state.register_logical_session, key, record=record)
                record.update(status=200, session_id=state.session_id)
            elif path == '/v1/tool/forecast':
                receipts = body.get('receipts')
                if not isinstance(receipts, list) or len(receipts) > 1024 or any(
                        not isinstance(r, str) or not 1 <= len(r) <= 256 for r in receipts):
                    raise ValueError('receipts must be a list of at most 1024 receipt strings')
                response = await self._forecast(state.session_id, receipts, disconnected, record)
                record.update(status=200, session_id=state.session_id)
            elif path.startswith('/v1/kb/'):
                if owner.knowledge_manager is None:
                    raise RuntimeError('host KB is not configured')
                if path.endswith('/observe'):
                    await self._callback(self._validate_observation, state, body, record=record)
                response = await self._knowledge(path.rsplit('/', 1)[1], state.session_id, body, record)
                record.update(status=200, session_id=state.session_id,
                              kb_status=response.get('status'), execution_id=body.get('execution_id'))
            else:
                owner._validate(body, state.session_id)
                record.update(session_id=state.session_id, execution_id=body['execution_id'])
                if path.endswith('/start'):
                    record['phase'] = body.get('phase', 'start')
                if path.endswith('/admit'):
                    if body.get('prediction_receipt'):
                        # A forecast lock held by another thread must not stop
                        # the ASGI loop from returning unrelated acknowledgements.
                        await self._callback(owner.forecaster.begin, state.session_id, body,
                                             owner.forecaster.clock(), record=record)
                    if (owner.knowledge_manager is not None and body.get('execution_scope', 'agent-tool') == 'agent-tool'
                            and body.get('operation') != 'filesystem'):
                        body['prediction'] = await self._knowledge('claim', state.session_id, body, record)
                    response, duplicate = await self._callback(state.admission, body, admission=True, record=record)
                    admitted = not duplicate
                elif path.endswith('/start'):
                    response, duplicate = await self._callback(state.execution_start, body, record=record)
                else:
                    response, duplicate = await self._callback(state.completion, body, record=record)
                response.update(session_id=state.session_id, execution_id=body['execution_id'], duplicate=duplicate)
                record.update(status=200, duplicate=duplicate)
            # Let a disconnect already delivered by the socket reach its watcher.
            await asyncio.sleep(0)
            if disconnected.is_set():
                raise ConnectionResetError('client disconnected while control operation completed')
            record['response_ready_seconds'] = time.monotonic() - record['request_start']
            await self._json(send, 200, response)
        except (BrokenPipeError, ConnectionResetError) as exc:
            if admitted:
                await self._callback(state.abandon_admission, body)
                record['admission_rolled_back'] = True
            record.update(status=499, error_type=type(exc).__name__, error=str(exc))
        except Exception as exc:
            if path.endswith('/admit') and state is not None and isinstance(body, dict):
                def finish_unadmitted():
                    with state.condition:
                        if body.get('execution_id') not in state.executions:
                            owner.forecaster.finish(state.session_id, body.get('execution_id'))
                await self._callback(finish_unadmitted, record=record)
            status = (400 if isinstance(exc, (KeyError, TypeError, ValueError)) else
                      409 if isinstance(exc, RuntimeError) else 503)
            record.update(status=status, error_type=type(exc).__name__, error=str(exc))
            if not disconnected.is_set():
                await self._json(send, status, {'error': f'{type(exc).__name__}: {exc}'})
        finally:
            if watcher is not None:
                watcher.cancel()
            record['response_end'] = time.monotonic()
            record['latency_seconds'] = record['response_end']-record['request_start']
            with owner._lock:
                owner.requests.append(record)
            if (state is not None and state.request_timing is not None
                    and path in {'/v1/tool/admit', '/v1/tool/start', '/v1/tool/complete', '/v1/runtime/completion',
                                 '/v1/kb/predict', '/v1/kb/preview', '/v1/kb/observe'}):
                # Persist compact boundaries as requests finish, including on
                # an interrupted arm. Do not persist bodies, tokens or errors.
                fields = ('path', 'request_start', 'request_wall_time_ns', 'response_end',
                          'latency_seconds', 'body_received_seconds', 'response_ready_seconds',
                          'callback_queue_seconds', 'callback_seconds', 'knowledge_seconds', 'status', 'error_type',
                          'duplicate', 'execution_id', 'phase')
                state.request_timing({'event': 'policy_http_timing', 'session_id': state.session_id,
                                      **{key: record[key] for key in fields if key in record}})

    @staticmethod
    async def _json(send, status, value):
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        await send({'type': 'http.response.start', 'status': status,
                    'headers': [(b'content-type', b'application/json'),
                                (b'content-length', str(len(encoded)).encode())]})
        await send({'type': 'http.response.body', 'body': encoded})
