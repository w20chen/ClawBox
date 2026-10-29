from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import time

import pytest

from clawbox.experiments.admission import DemandLedger
from clawbox.experiments.memory import (
    LatestOnlyReporter, NodeMemorySampler, NumaCgroupMemorySampler,
    PeriodicCallback,
)
from clawbox.experiments.priority import PriorityDomain
from clawbox.experiments.snapshot_pool import WarmSnapshotPool
from clawbox.experiments.worker import (
    aggregate_admission_queue_snapshots, collect_session_results,
)


def test_blocked_topology_does_not_stop_memory_and_records_capture_time(tmp_path):
    entered, release, sampled = Event(), Event(), Event()
    def control():
        entered.set()
        assert release.wait(2)
    topology = PeriodicCallback(control, interval_s=.001, name='test-topology')
    memory = NodeMemorySampler(storage=tmp_path, interval_s=.01)
    observed = []
    def hook(*args):
        observed.append(dict(memory.last_observation))
        if len(observed) >= 3:
            sampled.set()
    memory.sample_hook = hook
    topology.start()
    memory.start()
    try:
        assert entered.wait(1)
        assert sampled.wait(1)
        assert observed[0]['observation_start_monotonic_s'] <= observed[0]['observation_end_monotonic_s']
        assert observed[-1]['observation_end_monotonic_s'] > observed[0]['observation_end_monotonic_s']
    finally:
        release.set()
        topology.stop()
        memory.stop()


def test_slow_hook_retains_capture_time_and_health_exposes_error(tmp_path):
    entered, release = Event(), Event()
    memory = NodeMemorySampler(storage=tmp_path, interval_s=.001)
    def hook(*args):
        entered.set()
        assert release.wait(2)
        raise ValueError('bad evidence')
    memory.sample_hook = hook
    memory.start()
    try:
        assert entered.wait(1)
        stamp = memory.last_observation['observation_end_monotonic_s']
        assert stamp <= time.monotonic()
    finally:
        release.set()
        memory._thread.join(1)
    with pytest.raises(RuntimeError, match='memory evidence sampling failed') as error:
        memory.check()
    assert str(error.value.__cause__) == 'bad evidence'
    with pytest.raises(RuntimeError, match='sampling failed'):
        memory.stop()


def test_latest_only_reporter_never_blocks_capture_and_keeps_newest_value():
    entered, release = Event(), Event()
    processed = []
    def report(value):
        processed.append(value)
        if value == 1:
            entered.set()
            assert release.wait(2)
    reporter = LatestOnlyReporter(report, name="test-memory-reporter")
    reporter.start()
    reporter.submit(1)
    assert entered.wait(1)
    started = time.monotonic()
    reporter.submit(2)
    reporter.submit(3)
    assert time.monotonic() - started < .1
    release.set()
    metrics = reporter.stop()
    assert processed == [1, 3]
    assert metrics["submitted"] == 3
    assert metrics["processed"] == 2
    assert metrics["coalesced"] == 1
    assert metrics["pending"] == 0


def test_latest_only_reporter_exposes_callback_failure():
    failed = Event()
    def report(_value):
        failed.set()
        raise ValueError("bad auxiliary report")
    reporter = LatestOnlyReporter(report, name="test-memory-reporter")
    reporter.start()
    reporter.submit(1)
    assert failed.wait(1)
    reporter._thread.join(1)
    with pytest.raises(RuntimeError, match="test-memory-reporter failed") as error:
        reporter.check()
    assert str(error.value.__cause__) == "bad auxiliary report"
    with pytest.raises(RuntimeError, match="test-memory-reporter failed"):
        reporter.stop()


def test_health_failure_detected_without_a_completed_session(monkeypatch):
    checks = []
    def check():
        checks.append(True)
        if len(checks) == 2:
            raise RuntimeError('sampler failed')
    monkeypatch.setattr('clawbox.experiments.worker.wait',
                        lambda pending, **kw: (set(), pending))
    with pytest.raises(RuntimeError, match='sampler failed'):
        collect_session_results({Future(): 0}, timeout=10, sessions=[], failures=[],
                                events=SimpleNamespace(write=lambda row: None),
                                arm_id='arm', check_health=check)
    assert len(checks) == 2


def test_topology_callback_failure_remains_a_policy_failure():
    def fail():
        raise OSError('affinity rejected')
    topology = PeriodicCallback(fail, interval_s=.001, name='test-topology')
    topology.start()
    topology._thread.join(1)
    with pytest.raises(RuntimeError, match='test-topology failed') as error:
        topology.check()
    assert str(error.value.__cause__) == 'affinity rejected'
    with pytest.raises(RuntimeError, match='test-topology failed'):
        topology.stop()


def test_numa_periodic_observation_reads_parent_once(tmp_path, monkeypatch):
    for name, text in {'memory.max': '1000', 'cpuset.mems.effective': '0-2',
                       'memory.current': '700',
                       'memory.numa_stat': 'active_anon N0=200 N1=100 N2=100\n',
                       'meminfo': 'MemTotal: 10000 kB\nMemAvailable: 7000 kB\n'}.items():
        (tmp_path/name).write_text(text)
    sampler = NumaCgroupMemorySampler(tmp_path, local_capacity_bytes=800,
        total_capacity_bytes=1000, local_numa_node=0, local_numa_nodes=(0, 1),
        shared_numa_node=2, meminfo=tmp_path/'meminfo', storage=tmp_path)
    reads = []
    original = Path.read_text
    def read(path, *args, **kwargs):
        reads.append(path.name)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    observation = sampler._capture_sample()
    assert reads.count('memory.current') == reads.count('memory.numa_stat') == 1
    assert observation['local_used_bytes'] == 600
    assert observation['shared_live_used_bytes'] == 100
    assert observation['combined_live_used_bytes'] == 700


@pytest.mark.parametrize('count', [1280, 2560])
def test_periodic_pending_reuses_observation_without_vm_reads(count):
    reads = []
    ledger = DemandLedger(sample=lambda: (0, 10000), capacity=10000)
    with ledger.condition:
        for i in range(count):
            ledger.add((str(i), 'tool'), scope='local', amount=100, local_amount=100)
            ledger.claims[str(i), 'tool'].sample = lambda: reads.append(True) or (20, 20)
    ledger.pending_observation()
    assert len(reads) == count
    reads.clear()
    row = ledger.pending_evidence('local')
    assert reads == []
    assert row['outstanding_bytes'] == count * 80
    assert row['outstanding_observation_age_seconds'] >= 0
    with ledger.condition:
        ledger.claims['0', 'tool'] = replace(ledger.claims['0', 'tool'])
    assert ledger.pending_evidence('local')['outstanding_bytes'] is None
    assert ledger.pending_evidence('local')['outstanding_observed_claims'] == count - 1


@pytest.mark.parametrize('count', [1280, 2560])
def test_pool_periodic_summary_does_not_serialize_manifests(count, monkeypatch):
    pool = WarmSnapshotPool(count * 10)
    pool._manifests = {i: SimpleNamespace(allocated_bytes=7) for i in range(count)}
    monkeypatch.setattr('clawbox.experiments.snapshot_pool.asdict',
                        lambda obj: pytest.fail('periodic sample serialized a manifest'))
    assert pool.usage_snapshot()['committed_bytes'] == count * 7


def test_periodic_queue_snapshot_marks_busy_domains_without_waiting():
    coordinator = SimpleNamespace(queue_snapshot=lambda **kwargs: None)
    row = aggregate_admission_queue_snapshots(
        {"node-a": SimpleNamespace(coordinator=coordinator)}, blocking=False,
    )
    assert row["measurement"] == "partial"
    assert row["unavailable_domains"] == ["node-a"]
    assert row["admission_queue_depth"] is None
    assert row["per_domain"]["node-a"] == {
        "measurement": "unavailable_admission_lock_busy",
    }


@pytest.mark.parametrize('count', [1280, 2560])
def test_c2_tick_uses_one_cpu_read_for_all_pending_vms(count, tmp_path, monkeypatch):
    reads = []
    def cpu_times(path):
        reads.append(True)
        return {cpu: (10 * len(reads), 20 * len(reads)) for cpu in range(16)}
    monkeypatch.setattr('clawbox.experiments.priority._cpu_times', cpu_times)
    domain = PriorityDomain(tmp_path, concurrency=count, repetition=0, legacy=False,
        topology_mode='c2', topology_min_duration_seconds=.001,
        topology_group_max_active=1, placements={i: ('0-15', 0) for i in range(count)})
    groups = ('0,2,4,6', '8,10,12,14')
    domain._groups_by_pool['0-15', 2, False] = groups
    domain._group_load = {g: 1 for g in groups}
    for i in range(count):
        domain.acquire_tool_call(i, str(i), vcpus=2)
        domain._pending_started[i, 'tool'] = 0
    reads.clear()
    changes = domain.sample_topology_placement()
    assert len(reads) == 1
    assert len(changes) == count
    assert all(e['action'] == 'bypass_full_numa' for e in changes)
