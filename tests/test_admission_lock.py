from threading import Condition, Event, Thread
import time

from clawbox.experiments.admission_lock import AdmissionLock, ProgressLock


def queued(lock, normal, progress):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with lock._guard:
            if len(lock._normal) == normal and len(lock._progress) == progress:
                return
        time.sleep(.001)
    raise AssertionError('lock contenders did not enqueue')


def test_progress_bypasses_slow_checks_without_starving_them():
    lock = AdmissionLock()
    progress = ProgressLock(lock)
    order = []
    threads = []

    def work(entrance, label):
        with entrance:
            order.append(label)

    with lock:
        for i in range(2):
            t = Thread(target=work, args=(lock, f'normal-{i}'))
            t.start(); threads.append(t)
        queued(lock, 2, 0)
        for i in range(10):
            t = Thread(target=work, args=(progress, f'progress-{i}'))
            t.start(); threads.append(t)
            queued(lock, 2, i + 1)
    for t in threads:
        t.join(2)
        assert not t.is_alive()
    assert order[:8] == [f'progress-{i}' for i in range(8)]
    assert order[8] == 'normal-0'
    assert order[-1] == 'normal-1'


def test_condition_wait_releases_all_recursion_and_restores_it():
    lock = AdmissionLock()
    condition = Condition(lock)
    progress = ProgressLock(lock)
    entered = Event()

    def notify():
        with progress:
            entered.set()
            condition.notify()

    with condition:
        with condition:
            t = Thread(target=notify)
            t.start()
            assert condition.wait(2)
            assert entered.is_set()
            assert lock._depth == 2
            assert lock._is_owned()
        assert lock._depth == 1
    t.join(2)
    assert not lock._is_owned()


def test_timed_out_waiter_does_not_receive_or_leak_ownership():
    lock = AdmissionLock()
    results = []
    with lock:
        t = Thread(target=lambda: results.append(lock.acquire(timeout=.01)))
        t.start(); t.join(2)
        assert results == [False]
        assert not lock._normal
        assert lock.acquire(blocking=False)  # recursive owner
        lock.release()
    t = Thread(target=lambda: (lock.acquire(), lock.release()))
    t.start(); t.join(2)
    assert not t.is_alive()
