"""Lease disjoint physical CPU sets for the lifetime of a Runtime/Tool pair."""
from contextlib import contextmanager
from pathlib import Path
from queue import Queue

from .topology import parse_cpu_list


class SessionCpuSlots:
    def __init__(self, slots, *, sysfs=Path('/sys/devices/system/cpu')):
        self.slots = tuple(slots)
        self.assigned = {}
        self.available = Queue()
        occupied = set()
        for slot in self.slots:
            physical = set()
            for cpu in parse_cpu_list(slot.cpus):
                path = sysfs / f'cpu{cpu}/topology/thread_siblings_list'
                try:
                    siblings = frozenset(parse_cpu_list(path.read_text().strip()))
                except OSError as exc:
                    raise RuntimeError(f'cannot verify physical CPU isolation: {path}') from exc
                physical.add(siblings)
            if physical & occupied:
                raise ValueError('session_cpu_slots must not share SMT siblings across slots')
            occupied.update(physical)
            self.available.put(slot)

    def for_session(self, index):
        return self.assigned.get(index, self.slots[index % len(self.slots)])

    @contextmanager
    def lease(self, index):
        slot = self.available.get()
        self.assigned[index] = slot
        try:
            yield slot
        finally:
            del self.assigned[index]
            self.available.put(slot)
