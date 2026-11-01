"""Fixed pair ownership in child processes, with durable I/O off the CPU lane."""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import queue
import threading
import time


@dataclass
class AdmissionMetadata:
    seed_path: str
    identities: dict
    providers: dict = field(default_factory=dict)

    def __call__(self, repository, models, command, digest):
        from clawbox.experiments.prediction import HostLivePredictionProvider
        if repository not in self.providers:
            self.providers[repository] = HostLivePredictionProvider(
                Path(self.seed_path), repository=repository,
                sandbox_identity=self.identities.get(repository))
        return self.providers[repository]._metadata({'models': models}, command, digest)


def _owner(connection, options, scheduling):
    from clawbox.experiments.host_kb import HostKnowledgeManager
    import gc
    import sys
    (gc.enable if scheduling['cyclic_gc_enabled'] else gc.disable)()
    sys.setswitchinterval(scheduling['thread_switch_interval_seconds'])
    inbox = queue.Queue()
    pending = defaultdict(deque)
    busy = set()
    from .preload import SEED
    if SEED is not None and SEED.path == Path(options['seed_path']).resolve():
        options = {**options, 'prepared_seed': SEED}
    manager = HostKnowledgeManager(**options)
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix='kb-journal')
    metric_root = manager.root / 'operations'
    metric_root.mkdir(exist_ok=True)
    metric_stream = (metric_root / f'owner-{os.getpid()}-{time.time_ns()}.jsonl').open('x')
    metric_cursor = 0

    def receive():
        try:
            while True:
                item = connection.recv()
                inbox.put(('request', item))
                if item[1] == 'close':
                    return
        except (EOFError, OSError):
            inbox.put(('disconnect', None))

    threading.Thread(target=receive, name='kb-inbox', daemon=True).start()
    connection.send(('ready', manager.identity))

    def reply(job, value=None, error=None):
        ident, operation, args, enqueued = job
        connection.send((ident, value, None if error is None else (type(error).__name__, str(error))))
        if args and operation not in {'summary', 'metrics', 'process_sample'}:
            session = args[0]
            if pending[session]:
                inbox.put(('continuation', pending[session].popleft()))
            else:
                busy.discard(session)

    def advance(job, transaction, started, cpu_used, failure=None):
        cpu = time.thread_time()
        try:
            commit = transaction.throw(failure) if failure else next(transaction)
        except StopIteration as done:
            manager.metrics.append(dict(operation=job[1], session_id=job[2][0],
                status=(done.value or {}).get('status', 'ok'), seconds=time.monotonic()-started,
                cpu_seconds=cpu_used+time.thread_time()-cpu))
            reply(job, done.value)
        except Exception as exc:
            manager.metrics.append(dict(operation=job[1], session_id=job[2][0], status='error',
                seconds=time.monotonic()-started, cpu_seconds=cpu_used+time.thread_time()-cpu))
            reply(job, error=exc)
        else:
            cpu_used += time.thread_time()-cpu
            submitted = time.monotonic()
            future = writer.submit(manager._append, *commit)
            future.add_done_callback(lambda f: inbox.put(
                ('commit', (job, transaction, started, cpu_used, submitted, f))))

    stopping = None
    try:
        while True:
            if len(manager.metrics) - metric_cursor >= 128:
                metric_stream.writelines(json.dumps(row, separators=(',', ':'))+'\n'
                                         for row in manager.metrics[metric_cursor:])
                metric_stream.flush()
                metric_cursor = len(manager.metrics)
            if stopping is not None and not busy and not any(pending.values()):
                reply(stopping, True)
                break
            kind, item = inbox.get()
            if kind == 'disconnect':
                break
            if kind == 'commit':
                job, transaction, started, cpu_used, submitted, future = item
                manager.metrics.append(dict(operation='journal_wait', session_id=job[2][0],
                    seconds=time.monotonic()-submitted, status='error' if future.exception() else 'ok'))
                advance(job, transaction, started, cpu_used, future.exception())
                continue
            job = item
            ident, operation, args, enqueued = job
            if operation == 'close':
                stopping = job
                continue
            session = args[0] if args else None
            if session is not None:
                if session in busy and kind != 'continuation':
                    pending[session].append(job)
                    continue
                busy.add(session)
            manager.metrics.append(dict(operation='owner_queue', session_id=session,
                seconds=time.monotonic()-enqueued, status='ok'))
            try:
                if operation == 'metrics':
                    reply(job, list(manager.metrics))
                else:
                    method = getattr(manager, operation)
                    if hasattr(method, 'prepare'):
                        advance(job, method.prepare(manager, *args), time.monotonic(), 0.)
                    else:
                        reply(job, method(*args))
            except Exception as exc:
                reply(job, error=exc)
    finally:
        writer.shutdown(wait=True)
        metric_stream.writelines(json.dumps(row, separators=(',', ':'))+'\n'
                                 for row in manager.metrics[metric_cursor:])
        metric_stream.close()
        connection.close()


class _Owner:
    def __init__(self, context, options, limit, scheduling):
        self.connection, child = context.Pipe()
        self.lock = threading.Lock()
        self.outbox = queue.Queue()
        self.capacity = threading.BoundedSemaphore(limit)
        self.pending = {}
        self.sequence = 0
        self.error = None
        self.closing = False
        self.process = context.Process(target=_owner, args=(child, options, scheduling), name='clawbox-kb', daemon=True)
        self.process.start()
        child.close()
        if not self.connection.poll(120):
            self.process.terminate()
            self.process.join(5)
            self.connection.close()
            raise RuntimeError('KB owner did not initialize within 120s; inspect seed and host resources')
        try:
            kind, self.identity = self.connection.recv()
            if kind != 'ready':
                raise RuntimeError('invalid KB owner startup acknowledgement')
        except BaseException:
            self.process.join(5)
            self.connection.close()
            raise
        self.reader = threading.Thread(target=self._receive, name='kb-replies', daemon=True)
        self.reader.start()
        self.sender = threading.Thread(target=self._send, name='kb-requests', daemon=True)
        self.sender.start()

    def _send(self):
        try:
            while True:
                job = self.outbox.get()
                if job is None:
                    return
                self.connection.send(job)
                if job[1] == 'close':
                    return
        except (EOFError, OSError):
            self._fail()

    def _fail(self):
        with self.lock:
            self.error = RuntimeError('KB owner exited; reconcile and resume the same attempt')
            pending, self.pending = self.pending, {}
        for future in pending.values():
            self.capacity.release()
            future.set_exception(self.error)

    def _receive(self):
        try:
            while True:
                ident, value, error = self.connection.recv()
                with self.lock:
                    future = self.pending.pop(ident, None)
                if future is None:
                    continue
                self.capacity.release()
                if error is None:
                    future.set_result(value)
                else:
                    cls = {'ValueError': ValueError, 'KeyError': KeyError}.get(error[0], RuntimeError)
                    future.set_exception(cls(f'{error[0]}: {error[1]}'))
        except (EOFError, OSError) as exc:
            self._fail()

    def submit(self, operation, *args):
        if not self.capacity.acquire(blocking=False):
            raise RuntimeError('KB owner queue is full; increase execution.kb_queue_capacity for a new run')
        future = Future()
        with self.lock:
            if self.error:
                self.capacity.release()
                raise self.error
            if self.closing:
                self.capacity.release()
                raise RuntimeError('KB owner is closing')
            if operation == 'close':
                self.closing = True
            self.sequence += 1
            ident = self.sequence
            self.pending[ident] = future
            self.outbox.put_nowait((ident, operation, args, time.monotonic()))
        return future

    def close(self):
        try:
            if self.process.is_alive() and self.error is None:
                self.submit('close').result(timeout=30)
        finally:
            self.process.join(5)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(5)
            self.connection.close()
            self.reader.join(5)
            self.outbox.put_nowait(None)
            self.sender.join(5)


class ProcessKnowledgeManager:
    """The existing manager interface; only compact requests cross processes."""
    def __init__(self, *, processes=4, queue_capacity=1024, cyclic_gc_enabled=False,
                 thread_switch_interval_seconds=.001, **options):
        if os.name != 'nt':
            os.environ['CLAWBOX_KB_PRELOAD_SEED'] = str(Path(options['seed_path']).resolve())
            multiprocessing.set_forkserver_preload(['clawbox.experiments.knowledge.preload'])
        context = multiprocessing.get_context('spawn' if os.name == 'nt' else 'forkserver')
        self.owners = []
        self.closed = False
        self.options = options
        try:
            # One manifest writer completes initialization before the next opens it.
            for _ in range(processes):
                self.owners.append(_Owner(context, options, queue_capacity, dict(
                    cyclic_gc_enabled=cyclic_gc_enabled,
                    thread_switch_interval_seconds=thread_switch_interval_seconds)))
        except BaseException:
            self.close()
            raise
        self.identity = self.owners[0].identity

    def _owner(self, session):
        index = int.from_bytes(hashlib.sha256(session.encode()).digest()[:8], 'big') % len(self.owners)
        return self.owners[index]

    def submit(self, operation, session_id, *args):
        if self.closed:
            raise RuntimeError('KB manager is closed')
        return self._owner(session_id).submit(operation, session_id, *args)

    async def request(self, operation, session_id, *args):
        # shield prevents a disconnected HTTP client from cancelling a commit.
        return await asyncio.shield(asyncio.wrap_future(self.submit(operation, session_id, *args)))

    def reserve(self, session_id, repository):
        return self.submit('reserve', session_id, repository).result()

    def predict(self, session_id, payload):
        return self.submit('predict', session_id, payload).result()

    def claim(self, session_id, payload):
        return self.submit('claim', session_id, payload).result()

    def observe(self, session_id, payload):
        return self.submit('observe', session_id, payload).result()

    def mark_degraded(self, session_id, execution_id, reason):
        return self.submit('mark_degraded', session_id, execution_id, reason).result()

    def scope_summary(self, session_id):
        return self.submit('scope_summary', session_id).result()

    def summary(self):
        futures = [owner.submit('summary') for owner in self.owners]
        values = [future.result() for future in futures]
        return {**values[0], 'scopes': {k: v for value in values for k, v in value['scopes'].items()},
                'journal_bytes': sum(v['journal_bytes'] for v in values),
                'kb_processes': len(values)}

    @property
    def metrics(self):
        futures = [owner.submit('metrics') for owner in self.owners]
        return [dict(row, owner_pid=owner.process.pid)
                for owner, future in zip(self.owners, futures) for row in future.result()]

    def process_sample(self):
        import psutil
        parent = psutil.Process()
        cpu = parent.cpu_times()
        children = []
        for owner in self.owners:
            try:
                process = psutil.Process(owner.process.pid)
                times = process.cpu_times()
                children.append(dict(pid=process.pid, rss_bytes=process.memory_info().rss,
                    cpu_seconds=times.user+times.system, threads=process.num_threads()))
            except psutil.NoSuchProcess:
                children.append(dict(pid=owner.process.pid, exited=True))
        return dict(measurement='worker_and_separate_kb_owners', rss_bytes=parent.memory_info().rss,
                    cpu_seconds=cpu.user+cpu.system, threads=parent.num_threads(), kb_owners=children)

    def close(self):
        if self.closed:
            return
        self.closed = True
        failures = []
        for owner in self.owners:
            try:
                owner.close()
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError('KB owner cleanup failed: ' + '; '.join(failures))
