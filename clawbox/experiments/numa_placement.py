"""Compute placement using the existing atomic memory-admission coordinators.

CPU commitments are offered vCPUs, never measured utilization. Memory claims
belong exclusively to PolicyCoordinator/DemandLedger and are consumed by the
worker's normal VM creation path.
"""
from __future__ import annotations

from pathlib import Path
import time

from .policy import AdmissionTimeout
from .topology import parse_cpu_list

MIB = 1024**2


class CpuMonitor:
    def __init__(self, cpus, *, proc=Path('/proc'), interval=.2):
        self.cpus = {key: parse_cpu_list(value) for key, value in cpus.items()}
        self.proc = proc
        self.interval = interval
        self.previous = None
        self.last = 0.0
        self.values = {key: {'cpu_utilization': None, 'scheduling_pressure': None} for key in cpus}
        self.sample()

    def sample(self):
        now = time.monotonic()
        if now - self.last < self.interval:
            return self.values
        stat = {}
        for line in (self.proc/'stat').read_text().splitlines():
            fields = line.split()
            if fields[0].startswith('cpu') and fields[0][3:].isdigit():
                values = list(map(int, fields[1:9]))  # guest time is already in user/nice
                stat[int(fields[0][3:])] = (sum(values), values[3] + values[4])
        scheduling = {}
        enabled = self.proc/'sys/kernel/sched_schedstats'
        if enabled.exists() and enabled.read_text().strip() == '1':
            for line in (self.proc/'schedstat').read_text().splitlines():
                fields = line.split()
                if fields[0].startswith('cpu') and fields[0][3:].isdigit() and len(fields) >= 10:
                    scheduling[int(fields[0][3:])] = (int(fields[7]), int(fields[8]))
        sample = {}
        for key, cpus in self.cpus.items():
            if not cpus <= stat.keys():
                raise RuntimeError(f'configured CPUs missing from /proc/stat: {key}')
            total = sum(stat[c][0] for c in cpus)
            idle = sum(stat[c][1] for c in cpus)
            run_wait = (tuple(sum(scheduling[c][i] for c in cpus) for i in (0, 1))
                        if cpus <= scheduling.keys() else None)
            sample[key] = total, idle, run_wait
            old = self.previous.get(key) if self.previous else None
            utilization = pressure = None
            if old and total > old[0] and idle >= old[1]:
                utilization = max(0., min(1., 1 - (idle-old[1])/(total-old[0])))
            if old and old[2] is not None and run_wait is not None:
                run, wait = (run_wait[i]-old[2][i] for i in (0, 1))
                if run >= 0 and wait >= 0 and run+wait > 0:
                    pressure = wait/(run+wait)
            self.values[key] = {'cpu_utilization': utilization, 'scheduling_pressure': pressure}
        self.previous, self.last = sample, now
        return self.values


class SessionPlacement:
    """Choose and reserve under the shared admission lock; keep placement stable."""
    def __init__(self, domains, *, cpu_overcommit, monitor, emit):
        self.domains = domains
        self.monitor = monitor
        self.emit = emit
        self.condition = next(iter(domains.values())).coordinator.demand.condition
        self.capacity = {key: len(parse_cpu_list(d.cpus))*cpu_overcommit for key, d in domains.items()}
        self.sessions = {}
        self.waiting = set()
        self.template_affinity_decisions = 0
        self.template_cache_hits = 0

    def acquire(self, session_id, *, vcpus, memory_mib, timeout, fixed=None,
                template_id=None, template_aware=False,
                template_sharing_supported=False, balance_slack=0.10):
        with self.condition:
            if session_id in self.sessions or session_id in self.waiting:
                raise RuntimeError(f'duplicate session placement: {session_id}')
            self.waiting.add(session_id)
        try:
            return self._acquire(
                session_id, vcpus=vcpus, memory_mib=memory_mib,
                timeout=timeout, fixed=fixed, template_id=template_id,
                template_aware=template_aware,
                template_sharing_supported=template_sharing_supported,
                balance_slack=balance_slack,
            )
        finally:
            with self.condition:
                self.waiting.remove(session_id)

    def _acquire(self, session_id, *, vcpus, memory_mib, timeout, fixed=None,
                 template_id=None, template_aware=False,
                 template_sharing_supported=False, balance_slack=0.10):
        started = time.monotonic()
        keys = [fixed] if fixed is not None else list(self.domains)
        if not any(vcpus <= self.capacity[k] for k in keys):
            raise ValueError('VM pair exceeds placement CPU capacity; increase execution.placement_cpu_overcommit or compute CPUs')
        if not any(
            memory_mib * MIB + self.domains[k].coordinator.operation_headroom_bytes
            <= self.domains[k].coordinator.budget_bytes
            for k in keys
        ):
            raise ValueError(
                'VM pair plus policy operation headroom exceeds placement memory budget; '
                'increase the eligible compute-node HIGH watermark'
            )
        while True:
            with self.condition:
                remaining = timeout-(time.monotonic()-started)
                if remaining <= 0:
                    raise AdmissionTimeout(f'NUMA placement timed out for {session_id}')
                cpu = self.monitor.sample()
                candidates = []
                for key in keys:
                    domain = self.domains[key]
                    c = domain.coordinator
                    offered = sum(
                        item[1] for item in self.sessions.values() if item[0] == key
                    )
                    if offered+vcpus > self.capacity[key]:
                        continue
                    used = c.physical_sample()[0]
                    pending = c.demand.pending(c.demand_scope)
                    memory_pressure = (used+pending+memory_mib*MIB+c.operation_headroom_bytes)/c.budget_bytes
                    measured = [v for v in cpu[key].values() if v is not None]
                    cpu_pressure = max([offered/self.capacity[key], *measured])
                    reasons = c._pressure_reasons(memory_mib*MIB+c.startup_headroom_bytes)
                    if c._new_session_admission_blocked:
                        reasons += ('local_high_hysteresis',)
                    same_template_peers = sum(
                        item[0] == key and item[2] == template_id
                        for item in self.sessions.values()
                    )
                    evidence = dict(cpu[key], offered_vcpus=offered, cpu_capacity=self.capacity[key],
                                    resident_bytes=used, outstanding_bytes=pending,
                                    memory_budget_bytes=c.budget_bytes, safety_reasons=list(reasons))
                    evidence.update(
                        template_id=template_id,
                        same_template_peers=same_template_peers,
                        template_sharing_supported=template_sharing_supported,
                    )
                    candidates.append((bool(reasons), (cpu_pressure+memory_pressure)/2, key, evidence))
                if not candidates:
                    self.condition.wait(min(.2, remaining))
                    continue
                # A feasible node always wins. With none feasible, use the
                # existing admission queue/reclamation briefly, then resample
                # all nodes rather than permanently waiting on one domain.
                feasible = [item for item in candidates if not item[0]]
                ranked = feasible or candidates
                best_pressure = min(item[1] for item in ranked)
                affinity_applied = bool(
                    fixed is None and template_aware and template_id
                    and template_sharing_supported and feasible
                )
                if affinity_applied:
                    balanced = [
                        item for item in feasible
                        if item[1] <= best_pressure + balance_slack
                    ]
                    _, score, key, evidence = min(
                        balanced,
                        key=lambda item: (
                            -item[3]["same_template_peers"],
                            item[1], item[2],
                        ),
                    )
                else:
                    _, score, key, evidence = min(ranked, key=lambda x: x[:3])
                c = self.domains[key].coordinator
                self.sessions[session_id] = key, vcpus, template_id
                if template_aware:
                    self.template_affinity_decisions += 1
                if affinity_applied and evidence["same_template_peers"]:
                    self.template_cache_hits += 1
            try:
                c.acquire(session_id, memory_mib, min(.2, remaining), wait_class='create')
            except AdmissionTimeout:
                with self.condition:
                    self.sessions.pop(session_id)
                    self.condition.notify_all()
                continue
            except BaseException:
                with self.condition:
                    self.sessions.pop(session_id)
                    self.condition.notify_all()
                raise
            try:
                self.emit({'event':'session_placement', 'session_id':session_id, 'compute_domain':key,
                           'score':score, 'wait_seconds':time.monotonic()-started,
                           'template_aware_requested':template_aware,
                           'template_affinity_applied':affinity_applied,
                           'template_affinity_reason':(
                               'verified_shared_base' if affinity_applied else
                               'sharing_not_verified' if template_aware and not template_sharing_supported else
                               'disabled_or_no_template'
                           ),
                           'candidates':{item[2]:item[3] for item in candidates}, **evidence})
            except BaseException:
                c.release(session_id, memory_mib, wait_class='create')
                with self.condition:
                    self.sessions.pop(session_id)
                    self.condition.notify_all()
                raise
            return key

    def sample(self):
        with self.condition:
            cpu = self.monitor.sample()
            result = {}
            for key, domain in self.domains.items():
                c = domain.coordinator
                resident = c.physical_sample()[0]
                pending = c.demand.pending(c.demand_scope)
                result[key] = dict(cpu[key], resident_bytes=resident,
                    outstanding_bytes=pending, remaining_budget_bytes=c.budget_bytes-resident-pending,
                    waiting_sessions_global=len(self.waiting),
                    admission_queue_depth=len(c._waiters),
                    cpu_psi=None, bandwidth_utilization=None,
                    admitted_sessions=sum(item[0] == key for item in self.sessions.values()),
                    running_vms=sum(int(s.lifecycle.resident) + int(s.runtime_lifecycle is not None and s.runtime_lifecycle.resident)
                                    for s in c._sessions.values()))
            return result

    def release(self, session_id):
        with self.condition:
            self.sessions.pop(session_id, None)
            self.condition.notify_all()
