from pathlib import Path
from types import SimpleNamespace

import pytest

from clawbox.experiments.baselines import resolve_baseline
from clawbox.experiments.memory import cgroup_memory_evidence, NodeMemorySampler
from clawbox.experiments.policy import PolicyCoordinator, AdmissionTimeout
from clawbox.experiments.spec import ExperimentSpec, expand_matrix
from test_experiments_v2 import raw_spec

MIB = 1024**2


def test_os_creation_has_no_hidden_admission_budget():
    free = [100 * MIB]
    c = PolicyCoordinator(resolve_baseline('default-resident').as_policy(),
        budget_mib=1, emergency_free_mib=1, operation_headroom_mib=100,
        os_managed_lifecycle=True, physical_sample=lambda: (50 * MIB, free[0]))
    assert c.acquire('s', 0, 0, wait_class='create') == 0
    assert not c.demand.claims and not c._waiters
    free[0] = 0
    with pytest.raises(AdmissionTimeout, match='emergency'):
        c.acquire('s', 0, 0, wait_class='create')


def test_zero_lifecycle_claim_still_checks_safety_and_releases():
    used = [0, 100*MIB]
    c = PolicyCoordinator(resolve_baseline('uncontrolled-resident').as_policy(),
        budget_mib=1, emergency_free_mib=1, operation_headroom_mib=0,
        physical_sample=lambda: tuple(used))
    c.acquire('s', 0, 0, wait_class='create')
    assert ('s', 'create') in c.demand.claims
    c.release('s', 0, wait_class='create')
    assert not c.demand.claims
    used[1] = 0
    with pytest.raises(AdmissionTimeout):
        c.acquire('s', 0, 0, wait_class='create')
    assert not c.demand.claims


def test_os_managed_is_shared_by_resident_arms_and_rejects_snapshot():
    raw = raw_spec()
    raw['policies'] = [resolve_baseline(n).as_policy().model_dump(mode='json')
        for n in ('uncontrolled-resident', 'fixed-resident', 'predicted-resident')]
    raw['resources'].update(lifecycle_admission='os-managed', proactive_cache_reclaim=False, static_tool_memory_mib=256)
    spec = ExperimentSpec.model_validate(raw)
    assert all(a.resources.lifecycle_reservation_mib(8192) == 0 for a in expand_matrix(spec))
    raw['resources']['proactive_cache_reclaim'] = True
    with pytest.raises(ValueError, match='proactive_cache_reclaim'):
        ExperimentSpec.model_validate(raw)
    raw['resources']['proactive_cache_reclaim'] = False
    raw['policies'].append(resolve_baseline('predicted-wait-snapshot').as_policy().model_dump(mode='json'))
    with pytest.raises(ValueError, match='resident policies'):
        ExperimentSpec.model_validate(raw)


def test_kernel_evidence_preserves_unknowns_and_overlapping_counters(tmp_path):
    (tmp_path/'memory.current').write_text('1000')
    (tmp_path/'memory.stat').write_text('anon 200\nfile 700\nshmem 600\npgscan 5\n')
    (tmp_path/'memory.pressure').write_text('some avg10=0.50 avg60=0.10 avg300=0.00 total=123\n')
    evidence = cgroup_memory_evidence(tmp_path)
    assert evidence['current_bytes'] == 1000
    assert evidence['stat']['file'] == 700 and evidence['stat']['shmem'] == 600
    assert evidence['swap_current_bytes'] is None
    assert evidence['events'] is None
    assert evidence['pressure']['some']['total'] == 123


def test_sampling_failure_cannot_look_like_success(tmp_path):
    import time
    sampler = NodeMemorySampler(storage=tmp_path, interval_s=.001)
    def fail(*_):
        raise OSError('counter disappeared')
    sampler.sample_hook = fail
    sampler.start()
    sampler._thread.join(timeout=1)
    with pytest.raises(RuntimeError, match='sampling failed'):
        sampler.stop()


def test_borrow_candidate_uses_local_residency_not_configured_capacity():
    from clawbox.experiments.worker import WatermarkController
    from threading import Lock
    c = object.__new__(WatermarkController)
    large = SimpleNamespace(resident=True, sandbox_id='large', configured_memory_bytes=1000)
    small = SimpleNamespace(resident=True, sandbox_id='small', configured_memory_bytes=500)
    large.observe_borrow_candidate = lambda: SimpleNamespace(local_bytes=10)
    small.observe_borrow_candidate = lambda: SimpleNamespace(local_bytes=400)
    c._lock = Lock()
    c._lifecycles = {'session': (large, small)}
    c.shared_pool = SimpleNamespace(borrow_headroom_bytes=1000)
    c.borrower = SimpleNamespace(borrowed=lambda: (), allocates_shared=lambda sid: True, observe_resident=lambda sid:
        SimpleNamespace(local_bytes={'large': 10, 'small': 400}[sid]))
    assert c._next_borrow_candidate() is small
    c.borrower.borrowed = lambda: ('small',)
    c.borrower.allocates_shared = lambda sid: False  # retained reservation, LOCAL policy
    assert c._next_borrow_candidate() is small
    c.borrower.allocates_shared = lambda sid: True
    assert c._next_borrow_candidate() is large
    c.borrower.borrowed = lambda: ()
    small.observe_borrow_candidate = lambda: None  # checkpoint won after candidate enumeration
    assert c._next_borrow_candidate() is large


def test_memory_trace_is_loadable_and_never_overwrites(tmp_path):
    import json
    import subprocess
    import sys
    from clawbox.replay.trace import load_trace
    trace = tmp_path/'phases.jsonl'
    command = [sys.executable, 'scripts/make-memory-reclaim-trace.py', str(trace),
               '--allocation-mib', '1', '--hold-seconds', '.01', '--settle-seconds', '.01']
    subprocess.run(command, check=True, capture_output=True)
    actions = load_trace(trace)
    assert len(actions) == 2
    contents = trace.read_bytes()
    repeated = subprocess.run(command, capture_output=True, text=True)
    assert repeated.returncode != 0 and 'reuse the existing trace' in repeated.stderr
    assert trace.read_bytes() == contents
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    import shlex
    payload = json.loads(records[1]['output']['content']['tool_calls'][0]['function']['arguments'])
    code = shlex.split(payload['command'])[-1]
    compile(code, '<generated-memory-probe>', 'exec')
    assert 'memory-reclaim-phases.json' in code


def test_zero_claim_placement_has_no_configured_capacity_gate():
    from clawbox.experiments.numa_placement import SessionPlacement
    c = PolicyCoordinator(resolve_baseline('uncontrolled-resident').as_policy(),
        budget_mib=1, emergency_free_mib=0, operation_headroom_mib=0,
        physical_sample=lambda: (0, 100*MIB))
    placement = SessionPlacement({'node': SimpleNamespace(coordinator=c, cpus='0')},
        cpu_overcommit=2, monitor=SimpleNamespace(sample=lambda: {'node': {}}), emit=lambda row: None)
    assert placement.acquire('s', vcpus=2, memory_mib=0, timeout=1) == 'node'
    assert c.demand.pending() == 0
    c.release('s', 0, wait_class='create')
    placement.release('s')
    assert not c.demand.claims and not placement.sessions
