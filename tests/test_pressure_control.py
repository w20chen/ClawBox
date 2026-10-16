from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from clawbox.experiments.pressure import PressureControl, Phase, MIB
from clawbox.experiments.spec import PressureControlSpec
from clawbox.experiments.baselines import resolve_baseline
from clawbox.experiments.policy import PolicyCoordinator, AdmissionRequest
from clawbox.experiments.policy_control import _Session


class Feedback:
    blocked = True

    def resource_feedback(self):
        return {'memory_blocked': self.blocked}


def sample(group, tick=0):
    for name, value in {
        'memory.stat': f'anon {128*MIB}\nactive_file 0\npgscan {tick*100}\n',
        'memory.current': str(128*MIB), 'memory.events.local': 'oom_kill 0\n',
        'memory.pressure': f'some avg10=0.00 total={tick*500000}\n',
        'cpu.pressure': f'some avg10=0.00 total={tick*500000}\n',
        'cpu.stat': f'usage_usec {tick*500000}\n', 'cpuset.mems.effective': '0',
    }.items():
        (group/name).write_text(value)


def control(tmp_path, mode='adaptive_joint', **flags):
    sample(tmp_path)
    controller = PressureControl(tmp_path, mode=mode, options=PressureControlSpec(cooldown_seconds=.01), **flags)
    domain = Feedback()
    for role in ('runtime', 'tool'):
        leaf = tmp_path/role
        leaf.mkdir()
        sample(leaf)
        for name, value in {'cpu.weight': '100', 'memory.low': '0', 'memory.high': 'max'}.items():
            (leaf/name).write_text(value)
        controller.register('s', role, leaf, 0, domain)
        controller.generation('s', role, 'vm-'+role)
        controller.vms['s', role].cooldown_until = 0
    return controller, domain


def advance(controller, tmp_path, monkeypatch, tick):
    monkeypatch.setattr('clawbox.experiments.pressure.time.monotonic', lambda: 100+tick)
    for path in (tmp_path, tmp_path/'tool', tmp_path/'runtime'):
        sample(path, tick)
    controller.poll()


def test_ordering_roles_and_generation(tmp_path):
    c, _ = control(tmp_path)
    c.event('s', 'model_end', 'm')
    c.event('s', 'model_start', 'm')
    assert c.vms['s', 'runtime'].phase == Phase.INACTIVE
    c.event('s', 'model_start', 'm2')
    c.event('s', 'ready', 'e')
    assert c.vms['s', 'tool'].phase == Phase.READY
    c.event('s', 'start', 'e')
    c.event('s', 'start', 'e')
    assert c.vms['s', 'tool'].phase == Phase.TOOL_ACTIVE
    assert c.vms['s', 'runtime'].phase == Phase.LLM_WAITING
    c.event('s', 'complete', 'e')
    c.event('s', 'start', 'e')
    c.event('s', 'ready', 'e')
    assert c.vms['s', 'tool'].phase == Phase.LLM_WAITING
    c.generation('s', 'tool', 'new')
    assert c.vms['s', 'tool'].generation == 2
    assert c.vms['s', 'tool'].sandbox == 'new'


def test_low_pressure_no_leaf_scan(tmp_path, monkeypatch):
    c, d = control(tmp_path)
    d.blocked = False
    c.event('s', 'ready', 'e')
    c.event('s', 'start', 'e')
    monkeypatch.setattr(c, '_sample', lambda *_: pytest.fail('low-pressure leaf scan'))
    c.poll()
    assert (tmp_path/'tool'/'cpu.weight').read_text() == '100'


def test_memory_reclaim_budget_timeout_and_rollback(tmp_path, monkeypatch):
    c, d = control(tmp_path, 'phase_memcg')
    c.event('s', 'ready', 'e')
    c.event('s', 'start', 'e')
    advance(c, tmp_path, monkeypatch, 0)
    advance(c, tmp_path, monkeypatch, 1)
    assert int((tmp_path/'tool'/'memory.low').read_text()) == 128*MIB
    assert (tmp_path/'runtime'/'memory.low').read_text() == '0'
    assert (tmp_path/'tool'/'memory.high').read_text() == 'max'
    advance(c, tmp_path, monkeypatch, 4)
    assert (tmp_path/'tool'/'memory.low').read_text() == '0'
    assert c.vms['s', 'tool'].ineffective_memory
    advance(c, tmp_path, monkeypatch, 5)
    assert (tmp_path/'tool'/'memory.low').read_text() == '0'


def train_release(c):
    for i in range(3):
        c.execution_ready('s', str(i), ('template', 'command'))
        c.event('s', 'start', str(i))
        c.execution_complete('s', str(i), {
            'host_cgroup_baseline_bytes': 128*MIB,
            'host_cgroup_peak_bytes': 256*MIB,
            'host_cgroup_final_bytes': 128*MIB,
        }, 10., True)


@pytest.mark.parametrize('memory,cpu', [(False, False), (True, False), (False, True), (True, True)])
def test_explicit_controls_are_independent(tmp_path, monkeypatch, memory, cpu):
    c, _ = control(tmp_path, 'explicit', memory_protection=memory, cpu_release_boost=cpu)
    monkeypatch.setattr('clawbox.experiments.pressure.time.monotonic', lambda: 100.)
    train_release(c)
    c.execution_ready('s', 'new', ('template', 'command'))
    c.execution_admitted('s', 'new', 0)
    c.event('s', 'start', 'new')
    advance(c, tmp_path, monkeypatch, 0)
    advance(c, tmp_path, monkeypatch, 1)
    assert int((tmp_path/'tool'/'memory.low').read_text()) == (128*MIB if memory else 0)
    assert int((tmp_path/'tool'/'cpu.weight').read_text()) == (200 if cpu else 100)
    assert (tmp_path/'tool'/'memory.high').read_text() == 'max'
    c.event('s', 'complete', 'new')
    assert (tmp_path/'tool'/'memory.low').read_text() == '0'
    assert (tmp_path/'tool'/'cpu.weight').read_text() == '100'


def test_cpu_requires_measured_release_cpu_pressure_and_local_blocking(tmp_path, monkeypatch):
    c, d = control(tmp_path, 'release_cpu')
    monkeypatch.setattr('clawbox.experiments.pressure.time.monotonic', lambda: 100.)
    train_release(c)
    c.execution_ready('s', 'new', ('template', 'command'))
    c.execution_admitted('s', 'new', 0)
    c.event('s', 'start', 'new')
    advance(c, tmp_path, monkeypatch, 0)
    advance(c, tmp_path, monkeypatch, 1)
    assert (tmp_path/'tool'/'cpu.weight').read_text() == '200'
    assert (tmp_path/'runtime'/'cpu.weight').read_text() == '100'
    assert (tmp_path/'tool'/'memory.low').read_text() == '0'
    d.blocked = False
    advance(c, tmp_path, monkeypatch, 2)
    assert (tmp_path/'tool'/'cpu.weight').read_text() == '100'
    c.close()


def test_missing_release_evidence_does_not_boost(tmp_path, monkeypatch):
    c, _ = control(tmp_path, 'release_cpu')
    c.event('s', 'ready', 'e')
    c.event('s', 'start', 'e')
    advance(c, tmp_path, monkeypatch, 0)
    advance(c, tmp_path, monkeypatch, 1)
    assert (tmp_path/'tool'/'cpu.weight').read_text() == '100'


def test_failure_restores_modified_controls(tmp_path):
    c, _ = control(tmp_path)
    vm = c.vms['s', 'tool']
    vm.modified.add('cpu.weight')
    (vm.group/'cpu.weight').write_text('200')
    c.poll = lambda: (_ for _ in ()).throw(OSError('missing cgroup counter'))
    c.interval = .001
    c.start()
    c.thread.join(2)
    assert (vm.group/'cpu.weight').read_text() == '100'
    with pytest.raises(RuntimeError, match='failed'):
        c.check()
    with pytest.raises(RuntimeError, match='failed'):
        c.close()


class VM:
    resident = True
    configured_memory_bytes = 256*MIB
    timings = [{'operation': op, 'status': 'ok', 'service_seconds': 1.}
               for op in ('checkpoint', 'restore')]

    def __init__(self):
        self.charge = 256*MIB

    def checkpoint_and_evict(self, **_):
        self.resident = False
        self.charge = 0
        return 1.


def coordinator():
    c = PolicyCoordinator(resolve_baseline('pressure_lifecycle').as_policy(),
        budget_mib=256, emergency_free_mib=0, operation_headroom_mib=0,
        physical_sample=lambda: (256*MIB, 1024*MIB))
    c.release_options = PressureControlSpec()
    return c


def test_release_eligibility_local_measurement_and_runtime_safety():
    c = coordinator()
    tool, runtime = VM(), VM()
    c.register('idle', tool)
    c.register_runtime('idle', runtime)
    c.register_memory('idle', tool=lambda: (tool.charge, tool.charge), lifecycle=lambda: (0, 0))
    c.begin_model_wait('idle', 'model', 100)
    assert c._select_victim_locked(exclude='') is None  # no blocked demand
    ticket = object()
    c._waiters.append(ticket)
    c._waiter_requests[ticket] = AdmissionRequest('new', 128*MIB, 'create', False)
    c.set_tool_active('idle', True)
    assert c._select_victim_locked(exclude='') is None
    c.set_tool_active('idle', False)
    assert c.evict_one_for_watermark()
    assert not tool.resident and runtime.resident
    assert c.release_observations[-1]['local_released_bytes'] == 256*MIB
    assert not c.evict_one_for_watermark()


def test_no_shared_pool_credit_or_unmeasured_cost():
    c = coordinator()
    vm = VM()
    c.register('s', vm)
    c.register_memory('s', tool=lambda: (256*MIB, 0), lifecycle=lambda: (0, 0))
    c.begin_model_wait('s', 'm', 100)
    assert c._release_rank(c._sessions['s'], time.monotonic()) is None
    c.register_memory('s', tool=lambda: (256*MIB, 256*MIB), lifecycle=lambda: (0, 0))
    vm.timings = []
    assert c._release_rank(c._sessions['s'], time.monotonic()) is None


def test_existing_admission_retries_after_actual_tool_release():
    c = coordinator()
    tool, runtime = VM(), VM()
    c.physical_sample = lambda: (tool.charge, 1024*MIB)
    c.demand.sample = c.physical_sample
    c.register('idle', tool)
    c.register_runtime('idle', runtime)
    c.register_memory('idle', tool=lambda: (tool.charge, tool.charge), lifecycle=lambda: (0, 0))
    c.begin_model_wait('idle', 'm', 100)
    c.acquire('new', 128, 1, wait_class='create')
    assert not tool.resident and runtime.resident
    assert c.release_observations[-1]['local_released_bytes'] == 256*MIB
    c.release('new', 128, wait_class='create')


def test_start_endpoint_rejects_stale_generation_and_late_start():
    seen = []
    s = _Session('s', 'token', lambda _: {'sandbox_id': 'vm', 'epoch': 2},
                 lambda _: {}, start=lambda body: seen.append(body['execution_id']))
    request = {'execution_id': 'e', 'command_sha256': 'a'*64}
    s.admission(request)
    with pytest.raises(ValueError, match='endpoint'):
        s.execution_start({**request, 'endpoint_sandbox_id': 'vm', 'endpoint_epoch': 1})
    start = {**request, 'endpoint_sandbox_id': 'vm', 'endpoint_epoch': 2}
    s.execution_start(start)
    s.execution_start(start)
    s.execution_start({**start, 'phase': 'end'})
    s.execution_start(start)
    s.completion(request)
    s.execution_start(start)
    assert seen == ['e', 'e']


def test_six_modes_are_explicit_and_cgroup_ablations_resident():
    for name in ('no_control', 'agentcgroup_legacy', 'phase_memcg', 'release_cpu',
                 'pressure_lifecycle', 'adaptive_joint'):
        p = resolve_baseline(name).as_policy()
        assert p.runtime_control.value == name
        assert (p.reclamation.value == 'snapshot_pause') == p.runtime_control.coordinates_release
