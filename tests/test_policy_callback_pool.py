from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

import pytest

from clawbox.experiments.policy_callback_pool import CallbackPool


def test_callback_submission_ignores_unrelated_executor_thread_start(monkeypatch):
    pool = CallbackPool(1, 'test-boundary')
    entered, release = Event(), Event()
    original = Thread.start

    def delayed(thread):
        if thread.name.startswith('unrelated'):
            entered.set()
            assert release.wait(3)
        return original(thread)

    monkeypatch.setattr(Thread, 'start', delayed)
    unrelated = ThreadPoolExecutor(max_workers=1, thread_name_prefix='unrelated')
    submitter = Thread(target=lambda: unrelated.submit(lambda: None))
    submitter.start()
    try:
        assert entered.wait(1)
        assert pool.submit(lambda: 42).result(timeout=.5) == 42
    finally:
        release.set()
        submitter.join(3)
        unrelated.shutdown()
        pool.shutdown()


def test_shutdown_cancels_queued_callbacks_but_preserves_running_result():
    pool = CallbackPool(1, 'test-boundary')
    entered, release = Event(), Event()

    def running():
        entered.set()
        assert release.wait(3)
        return 7

    current = pool.submit(running)
    assert entered.wait(1)
    queued = pool.submit(lambda: pytest.fail('cancelled callback ran'))
    pool.shutdown(wait=False, cancel_futures=True)
    assert queued.cancelled()
    with pytest.raises(RuntimeError, match='shutdown'):
        pool.submit(lambda: None)
    release.set()
    assert current.result(timeout=1) == 7
    pool.shutdown()


def test_callback_exception_does_not_kill_the_lane():
    with CallbackPool(1, 'test-boundary') as pool:
        failed = pool.submit(lambda: 1/0)
        with pytest.raises(ZeroDivisionError):
            failed.result(timeout=1)
        assert pool.submit(lambda: 'next').result(timeout=1) == 'next'
