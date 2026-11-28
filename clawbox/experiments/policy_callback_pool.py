"""Callback lanes whose submission cannot create OS threads.

ThreadPoolExecutor.submit holds a process-wide shutdown lock while starting
workers. Unrelated VM work must not make the policy HTTP event loop wait on it.
"""
from concurrent.futures import Executor, Future
from queue import Empty, SimpleQueue
from threading import Lock, Semaphore, Thread


class CallbackPool(Executor):
    def __init__(self, workers, name):
        if workers < 1:
            raise ValueError('callback workers must be positive')
        self._queue = SimpleQueue()
        self._incoming = SimpleQueue()
        self._lock = Lock()
        self._closed = False
        self._cancel_pending = False
        self._pending = set()
        self._limit, self._name = workers, name
        self._idle = Semaphore(0)
        self._threads = []
        self._dispatcher = Thread(target=self._dispatch, name=name+'-dispatch', daemon=True)
        self._dispatcher.start()

    def submit(self, function, /, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError('cannot schedule callbacks after shutdown')
            future = Future()
            self._pending.add(future)
            self._incoming.put((future, function, args, kwargs))
            return future

    def _dispatch(self):
        while True:
            item = self._incoming.get()
            if item is None:
                for _ in self._threads:
                    self._queue.put(None)
                return
            if self._cancel_pending:
                item[0].cancel()
                continue
            # Grow lazily, like ThreadPoolExecutor, but neither submit nor OS
            # thread creation runs in the HTTP event loop or uses its global lock.
            if not self._idle.acquire(blocking=False) and len(self._threads) < self._limit:
                thread = Thread(target=self._run, name=f'{self._name}-{len(self._threads)}', daemon=True)
                self._threads.append(thread)
                thread.start()
            self._queue.put(item)

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, function, args, kwargs = item
            with self._lock:
                self._pending.discard(future)
                running = future.set_running_or_notify_cancel()
            if not running:
                self._idle.release()
                continue
            try:
                result = function(*args, **kwargs)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)
            self._idle.release()

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._cancel_pending = cancel_futures
                if cancel_futures:
                    for future in self._pending:
                        future.cancel()
                    self._pending.clear()
                    for queue in (self._incoming, self._queue):
                        while True:
                            try:
                                item = queue.get_nowait()
                            except Empty:
                                break
                            item[0].cancel()
                self._incoming.put(None)
        if wait:
            self._dispatcher.join()
            for thread in self._threads:
                thread.join()
