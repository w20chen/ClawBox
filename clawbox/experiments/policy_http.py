"""Async transport for the existing policy protocol, with separate callback lanes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import time

import uvicorn


class PolicyHTTPServer:
    request_queue_size = 4096

    def __init__(self, address, owner, *, admission_workers):
        self.owner = owner
        self.failure = None
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(address)
        self.socket.listen(self.request_queue_size)
        self.socket.setblocking(False)
        self.server_address = self.socket.getsockname()
        self.admissions = ThreadPoolExecutor(max_workers=admission_workers, thread_name_prefix='policy-admit')
        self.boundaries = ThreadPoolExecutor(max_workers=64, thread_name_prefix='policy-boundary')
        self.server = uvicorn.Server(uvicorn.Config(self, host=address[0], port=address[1],
            loop='asyncio', http='h11', lifespan='off', access_log=False, log_level='critical',
            timeout_keep_alive=5, timeout_graceful_shutdown=5, backlog=self.request_queue_size))

    def serve_forever(self):
        try:
            asyncio.run(self.server.serve(sockets=[self.socket]))
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
        self.admissions.shutdown(wait=False, cancel_futures=True)
        self.boundaries.shutdown(wait=False, cancel_futures=True)

    async def _callback(self, function, *args, admission=False):
        return await asyncio.get_running_loop().run_in_executor(
            self.admissions if admission else self.boundaries, function, *args)

    async def _knowledge(self, operation, session, payload):
        manager = self.owner.knowledge_manager
        if hasattr(manager, 'request'):
            return await manager.request(operation, session, payload)
        return await self._callback(getattr(manager, operation), session, payload)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return
        path, method = scope['path'], scope['method']
        owner = self.owner
        if method == 'GET' and path == '/v1/kb/health' and owner.knowledge_manager is not None:
            from .knowledge.protocol import PROTOCOL, MAX_BODY_BYTES
            await self._json(send, 200, dict(protocol_versions=[PROTOCOL], max_body_bytes=MAX_BODY_BYTES))
            return
        routes = {'/v1/tool/admit', '/v1/tool/complete', '/v1/tool/start', '/v1/kb/predict', '/v1/kb/observe'}
        if method != 'POST' or path not in routes:
            await self._json(send, 404, {'error': 'unknown control route'})
            return
        record = dict(path=path, request_start=time.monotonic())
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

            async def watch_disconnect():
                while True:
                    if (await receive())['type'] == 'http.disconnect':
                        disconnected.set()
                        return
            watcher = asyncio.create_task(watch_disconnect())
            if path.startswith('/v1/kb/'):
                if owner.knowledge_manager is None:
                    raise RuntimeError('host KB is not configured')
                if path.endswith('/observe'):
                    with state.condition:
                        execution = state.executions.get(body.get('execution_id'))
                        if execution is None or execution.admission is None:
                            raise ValueError('observation has no admitted execution')
                response = await self._knowledge(path.rsplit('/', 1)[1], state.session_id, body)
                record.update(status=200, session_id=state.session_id,
                              kb_status=response.get('status'), execution_id=body.get('execution_id'))
            else:
                owner._validate(body, state.session_id)
                record.update(session_id=state.session_id, execution_id=body['execution_id'])
                if path.endswith('/admit'):
                    if (owner.knowledge_manager is not None and body.get('execution_scope', 'agent-tool') == 'agent-tool'
                            and body.get('operation') != 'filesystem'):
                        body['prediction'] = await self._knowledge('claim', state.session_id, body)
                    response, duplicate = await self._callback(state.admission, body, admission=True)
                    admitted = not duplicate
                elif path.endswith('/start'):
                    response, duplicate = await self._callback(state.execution_start, body)
                else:
                    response, duplicate = await self._callback(state.completion, body)
                response.update(session_id=state.session_id, execution_id=body['execution_id'], duplicate=duplicate)
                record.update(status=200, duplicate=duplicate)
            # Let a disconnect already delivered by the socket reach its watcher.
            await asyncio.sleep(0)
            if disconnected.is_set():
                raise ConnectionResetError('client disconnected while control operation completed')
            await self._json(send, 200, response)
        except (BrokenPipeError, ConnectionResetError) as exc:
            if admitted:
                await self._callback(state.abandon_admission, body)
                record['admission_rolled_back'] = True
            record.update(status=499, error_type=type(exc).__name__, error=str(exc))
        except Exception as exc:
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

    @staticmethod
    async def _json(send, status, value):
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        await send({'type': 'http.response.start', 'status': status,
                    'headers': [(b'content-type', b'application/json'),
                                (b'content-length', str(len(encoded)).encode())]})
        await send({'type': 'http.response.body', 'body': encoded})
