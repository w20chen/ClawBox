from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from clawbox.experiments.cpu_slots import SessionCpuSlots
from clawbox.experiments.spec import SessionCpuSlot


def topology(root):
    for cpu in range(4):
        path = root / f'cpu{cpu}/topology/thread_siblings_list'
        path.parent.mkdir(parents=True)
        path.write_text('0-1' if cpu < 2 else '2-3')


def test_slots_reject_different_logical_cpus_on_same_physical_core(tmp_path):
    topology(tmp_path)
    with pytest.raises(ValueError, match='SMT siblings'):
        SessionCpuSlots([SessionCpuSlot(node_id='n0', cpus=str(i)) for i in (0, 1)], sysfs=tmp_path)


def test_slot_is_held_until_session_exits_and_released_after_failure(tmp_path):
    topology(tmp_path)
    slots = SessionCpuSlots([SessionCpuSlot(node_id='n0', cpus='0,2')], sysfs=tmp_path)
    entered, release, second = Event(), Event(), Event()
    def first():
        with slots.lease(0):
            entered.set()
            assert release.wait(5)
            raise RuntimeError('session failed')
    def next_session():
        with slots.lease(1):
            second.set()
            return slots.for_session(1).cpus
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first)
        assert entered.wait(5)
        b = pool.submit(next_session)
        assert not second.wait(.05)
        release.set()
        with pytest.raises(RuntimeError, match='session failed'):
            a.result(timeout=5)
        assert b.result(timeout=5) == '0,2'
    assert not slots.assigned
