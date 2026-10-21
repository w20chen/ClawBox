from concurrent.futures import ThreadPoolExecutor
from threading import Event, Semaphore
from types import SimpleNamespace

import pytest

from clawbox.experiments.worker import creation_slot


def test_creation_slot_precedes_reservation_but_does_not_limit_running_tasks():
    gate=Semaphore(1)
    first_entered,create_done,task_done,second_entered=Event(),Event(),Event(),Event()
    records=[]
    events=SimpleNamespace(write=records.append)
    def first():
        with creation_slot(gate,events,'first') as (_,_,release):
            first_entered.set()  # placement/reservation happens only inside the slot
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
