from types import SimpleNamespace

import pytest

from clawbox.experiments import runtime_qualification as module
from clawbox.cube import CubeSandboxClient


def test_runtime_qualification_uses_first_configured_compute_node():
    resources = SimpleNamespace(
        compute_nodes=(
            SimpleNamespace(numa_node=0, cpus="0-79"),
            SimpleNamespace(numa_node=1, cpus="80-159"),
        ),
        local_numa_node=None,
    )
    assert module._qualification_compute_placement(resources) == (0, "0-79")


@pytest.mark.parametrize('completed', [True, False])
def test_guest_probe_checks_progress_after_releasing_vm_throttle(tmp_path, monkeypatch, completed):
    parent = tmp_path / 'probe'
    parent.mkdir()
    (tmp_path / 'memory.max').write_text(str(4096 * module.MIB))
    (parent / 'memory.current').write_text(str(128 * module.MIB))
    for name in ('high', 'low'):
        (parent/name).mkdir()
        (parent/name/'memory.current').write_text(str(64 * module.MIB))
    closed = []
    started = []

    class Control:
        def __init__(self, *args, emit, **kwargs):
            self.emit = emit
            self.stats = {'upstream': {'activations': 1}}
            self.backend = SimpleNamespace(get_stats=lambda: {'activations': 1})
            self.module = SimpleNamespace(_read_memory_events=lambda p: {'high': 1, 'oom_kill': 0})

        def start(self):
            started.append(True)
            assert int((parent/'memory.max').read_text()) > 640 * module.MIB
            self.emit({'parent_current_bytes': 640 * module.MIB})

        def check(self):
            assert not closed

        def close(self):
            closed.append(True)

    class Commands:
        def __init__(self, amount):
            self.amount = amount

        def run(self, command, **kwargs):
            if command.startswith('awk '):
                output = str(2 * 1024 * 1024)
            elif command.startswith('nohup '):
                output = '123'
            else:
                assert not started or closed, 'VM RPC must not be required while LOW is throttled'
                (parent/'memory.current').write_text(str(640 * module.MIB))
                output = str(self.amount) if not started or completed else ''
            return SimpleNamespace(exit_code=0, stdout=output)

    monkeypatch.setattr(module, 'RuntimeMemoryControl', Control)
    monkeypatch.setattr('clawbox.cube.CubeSandboxClient', lambda: CubeSandboxClient(sandbox_class=object))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    domain = SimpleNamespace(parent=parent, high=parent/'high', low=parent/'low')
    lifecycles = [None, SimpleNamespace(sandbox=SimpleNamespace(commands=Commands(64 * module.MIB))),
                  None, SimpleNamespace(sandbox=SimpleNamespace(commands=Commands(512 * module.MIB)))]
    result = module.guest_pressure_probe(domain, lifecycles)
    assert result['passed'] is completed
    assert result['activation_verified'] and result['low_throttling_verified']
    assert result['completion_checked_after_control_release']
    assert closed == [True]
    assert (parent/'memory.max').read_text() == (tmp_path/'memory.max').read_text()


@pytest.mark.parametrize('real_throttling', [True, False])
def test_guest_probe_remeasures_disappearing_charge_but_still_requires_throttling(
        tmp_path, monkeypatch, real_throttling):
    import itertools
    parent = tmp_path / 'probe'
    parent.mkdir()
    (tmp_path/'memory.max').write_text(str(4096 * module.MIB))
    (parent/'memory.current').write_text(str(128 * module.MIB))
    for name in ('high', 'low'):
        (parent/name).mkdir()
        (parent/name/'memory.current').write_text(str(64 * module.MIB))
    controls = []

    class Control:
        def __init__(self, *args, emit, **kwargs):
            controls.append(self)
            self.closed = False
            self.stats = {'upstream': {'activations': 1}}
            self.backend = SimpleNamespace(get_stats=lambda: {'activations': 1})
            self.module = SimpleNamespace(_read_memory_events=lambda p: {
                'high': int(real_throttling and len(controls) == 2), 'oom_kill': 0})

        def start(self):
            self.cap = int((parent/'memory.max').read_text())

        def check(self):
            (parent/'memory.current').write_text(str(640 * module.MIB))

        def close(self):
            self.closed = True

    class Commands:
        def __init__(self, amount):
            self.amount = amount

        def run(self, command, **kwargs):
            if command.startswith('awk '):
                output = str(2 * 1024 * 1024)
            elif command.startswith('nohup '):
                output = '123'
            else:
                assert all(c.closed for c in controls), 'release LOW before guest RPC'
                if not controls:
                    (parent/'memory.current').write_text(str(1024 * module.MIB))
                output = str(self.amount)
            return SimpleNamespace(exit_code=0, stdout=output)

    monkeypatch.setattr(module, 'RuntimeMemoryControl', Control)
    monkeypatch.setattr('clawbox.cube.CubeSandboxClient', lambda: CubeSandboxClient(sandbox_class=object))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(ticks))
    ticks = itertools.count()
    domain = SimpleNamespace(parent=parent, high=parent/'high', low=parent/'low')
    lifecycles = [None, SimpleNamespace(sandbox=SimpleNamespace(commands=Commands(64 * module.MIB))),
                  None, SimpleNamespace(sandbox=SimpleNamespace(commands=Commands(512 * module.MIB)))]
    result = module.guest_pressure_probe(domain, lifecycles)
    assert len(controls) == 2 and all(c.closed for c in controls)
    assert controls[1].cap < controls[0].cap
    assert result['passed'] is real_throttling
    assert [r['charged_bytes'] for r in result['budget_observations']] == [1024 * module.MIB, 640 * module.MIB]
    assert result['observed_peak_bytes'] >= 1024 * module.MIB
    assert (parent/'memory.max').read_text() == (tmp_path/'memory.max').read_text()
