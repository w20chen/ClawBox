from concurrent.futures import ThreadPoolExecutor
from threading import Event, Semaphore
from types import SimpleNamespace

import pytest

from clawbox.experiments.worker import creation_slot


def test_creation_slot_does_not_limit_running_tasks():
    gate=Semaphore(1)
    first_entered,create_done,task_done,second_entered=Event(),Event(),Event(),Event()
    records=[]
    events=SimpleNamespace(write=records.append)
    def first():
        with creation_slot(gate,events,'first') as (_,_,release):
            first_entered.set()
            assert create_done.wait(1)
            release()  # VM pair ready; task is still running
            assert task_done.wait(1)
    def second():
        with creation_slot(gate,events,'second'):
            second_entered.set()
    with ThreadPoolExecutor(2) as workers:
        a=workers.submit(first)
        assert first_entered.wait(1)
        b=workers.submit(second)
        try:
            assert not second_entered.wait(.03)
            create_done.set()
            assert second_entered.wait(1)
            assert not a.done()
        finally:
            create_done.set();task_done.set()
        a.result();b.result()
    assert gate.acquire(blocking=False)
    assert not gate.acquire(blocking=False)  # no double release at task completion
    assert sum(e['event']=='sandbox_create_gate_wait_started' for e in records)==2
    releases = [e for e in records if e['event']=='sandbox_create_gate_released']
    assert len(releases) == 2
    assert all(e['held_seconds'] >= 0 for e in releases)


def test_pre_vm_failure_releases_creation_slot():
    gate=Semaphore(1)
    with pytest.raises(ValueError,match='bad input'):
        with creation_slot(gate,SimpleNamespace(write=lambda e:None),'failed'):
            raise ValueError('bad input')
    assert gate.acquire(blocking=False)


def test_preparation_can_wait_for_memory_while_ready_pair_creates():
    prepare, create = Semaphore(1), Semaphore(1)
    first_creating, memory_waiting, release_create, release_memory = (Event() for _ in range(4))
    records = []
    events = SimpleNamespace(write=records.append)

    def session(name, memory_ready, started, finished):
        with creation_slot(prepare, events, name, phase='sandbox_prepare') as prep:
            if not memory_ready.is_set():
                memory_waiting.set()
            assert memory_ready.wait(3)
            with creation_slot(create, events, name):
                prep[2]()
                started.set()
                assert finished.wait(3)

    ready = Event()
    ready.set()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(session, 'ready', ready, first_creating, release_create)
        try:
            assert first_creating.wait(1)
            second = pool.submit(session, 'blocked', release_memory, Event(), ready)
            assert memory_waiting.wait(1)
            release_create.set()
            first.result(timeout=1)
            assert create.acquire(blocking=False)
            create.release()
            assert not second.done()
        finally:
            release_create.set()
            release_memory.set()
        second.result(timeout=2)
    assert prepare.acquire(blocking=False)
    assert not prepare.acquire(blocking=False)
    acquired = [e['session_id'] for e in records if e['event'] == 'sandbox_create_gate_acquired']
    assert acquired == ['ready', 'blocked']
