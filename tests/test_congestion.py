import hashlib
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from clawbox.experiments import congestion as c


@pytest.fixture
def control(tmp_path, monkeypatch):
    parent=tmp_path/'cg';parent.mkdir()
    (parent/'memory.max').write_text(str(6*1024**3))
    (parent/'memory.current').write_text(str(500*c.MIB))
    (parent/'memory.stat').write_text('anon 0\nshmem 0\npgscan 0\npgsteal 0\n')
    (parent/'memory.events').write_text('oom_kill 7\n')
    (parent/'memory.pressure').write_text('some avg10=0 total=0\n')
    high,low=parent/'high',parent/'low'
    group=high/'session'/'tool'
    for p in (high,low,group):
        p.mkdir(parents=True,exist_ok=True)
        for k,v in {'memory.stat':'anon 0\nshmem 0\n','memory.low':'17','memory.high':'999',
                    'memory.events':'high 0\noom 0\noom_kill 0\n'}.items():
            (p/k).write_text(v)
    controller=c.RuntimeMemoryControl(parent,high=high,low=low,interval=.01)
    yield controller,group
    controller.error=None
    controller.close()


def test_pinned_source_is_byte_identical():
    assert hashlib.sha256(c.UPSTREAM_SOURCE.read_bytes()).hexdigest() == c.UPSTREAM_SHA256


def test_oom_counter_is_a_domain_delta_even_when_runtime_control_is_off(control):
    original, _ = control
    rt = c.RuntimeMemoryControl(original.parent, high=original.high, low=original.low, enabled=False)
    (original.parent/'memory.events').write_text('oom_kill 9\n')
    rt.close()
    assert rt.stats['oom_kill_events'] == 2
    assert rt.stats['polls'] == 0


def test_runtime_off_does_not_enforce_even_under_pressure(control):
    original, _ = control
    rt = c.RuntimeMemoryControl(original.parent, high=original.high, low=original.low, enabled=False)
    before = rt.saved.copy()
    (rt.parent / 'memory.current').write_text(str(rt.capacity))
    try:
        rt._poll_backend()
        assert rt.backend is None
        assert rt.stats['polls'] == 0
        for group, (low, high) in before.items():
            assert (group / 'memory.low').read_text() == low
            assert (group / 'memory.high').read_text() == high
    finally:
        rt.close()


def test_runtime_tuning_is_applied_and_recorded(control):
    original, _ = control
    rt = c.RuntimeMemoryControl(
        original.parent, high=original.high, low=original.low,
        interval=.25, event_threshold=7, pressure_ratio=.93,
        protection_window_seconds=.4,
    )
    try:
        assert rt.module.CgroupMemcgController.PRESSURE_RATIO == .93
        assert rt.provenance['interval_seconds'] == .25
        assert rt.provenance['threshold'] == 7
        assert rt.provenance['pressure_ratio'] == .93
        assert rt.provenance['protection_window_seconds'] == .4
        rt.start()
        assert rt.backend._config.threshold == 7
        assert rt.backend._config.protection_window_s == .4
    finally:
        rt.close()


def test_tool_membership_does_not_reset_upstream_window(control, monkeypatch):
    rt,group=control
    now=[10.]
    monkeypatch.setattr(c.time,'monotonic',lambda:now[0])
    rt.backend.attach(rt.module.MemcgConfig(high_cgroup=str(rt.high),low_cgroups=[str(rt.low)]))
    rt.attached=True
    (rt.parent/'memory.current').write_text(str(int(rt.capacity*.90)))
    rt._poll_backend()
    assert rt.backend.get_stats()['activations']==1
    backend=rt.backend
    (rt.parent/'memory.current').write_text('0')
    (group/'memory.current').write_text('100')
    assert rt.backend is backend and rt.backend.get_stats()['protection_active']
    now[0]=10.99;rt._poll_backend()
    assert (rt.high/'memory.low').read_text()==str(int(rt.capacity*.8))
    assert (rt.low/'memory.high').read_text()==str(int(rt.capacity*.5))
    now[0]=11.;rt._poll_backend()
    assert rt.backend.get_stats()['activations']==1
    assert not rt.backend.get_stats()['protection_active']


def test_original_and_adapter_make_identical_decisions(control,tmp_path,monkeypatch):
    import importlib.util
    import shutil
    import sys
    rt,_=control
    original=tmp_path/'original'
    shutil.copytree(rt.parent,original)
    spec=importlib.util.spec_from_file_location('_unmodified_agentcg_test',c.UPSTREAM_SOURCE)
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    backend=module.CgroupMemcgController()
    clock=[0.]
    monkeypatch.setattr(c.time,'monotonic',lambda:clock[0])
    backend.attach(module.MemcgConfig(high_cgroup=str(original/'high'),low_cgroups=[str(original/'low')]))
    rt.backend.attach(rt.module.MemcgConfig(high_cgroup=str(rt.high),low_cgroups=[str(rt.low)]))
    rt.attached=True
    try:
        # No pressure, high event, active window ignoring changing signals,
        # expiration, PSI, and usage threshold; all in upstream precedence order.
        for stamp,high,psi,ratio in [(0,0,0,.2),(.1,1,0,.2),(.5,4,3,.9),
                                      (1.1,4,3,.9),(1.2,4,3,.1),(2.2,4,3,.1),
                                      (2.3,4,10,.1),(3.4,4,10,.1),(3.5,4,10,.85)]:
            clock[0]=stamp
            for root in (rt.parent,original):
                (root/'high'/'memory.events').write_text(f'high {high}\n')
                (root/'memory.pressure').write_text(f'some total={psi}\n')
                (root/'memory.current').write_text(str(int(rt.capacity*ratio)))
            backend.poll();rt._poll_backend()
            assert backend.get_stats()==rt.backend.get_stats()
            for branch,name in [('high','memory.low'),('low','memory.high')]:
                assert (original/branch/name).read_text()==(rt.parent/branch/name).read_text()
    finally:
        backend.detach();sys.modules.pop(spec.name,None)


def test_priority_assignment_balances_across_paired_repetitions():
    from clawbox.experiments.priority import session_priority
    for concurrency in (1,2,5):
        highs=[]
        for repetition in range(concurrency):
            selected=[i for i in range(concurrency) if session_priority(i,concurrency,repetition)=='high']
            assert len(selected)==1
            highs.extend(selected)
        assert highs==list(range(concurrency))


def test_kernel_without_psi_keeps_original_event_and_usage_signals(control):
    rt,_=control
    (rt.parent/'memory.pressure').unlink()
    (rt.parent/'memory.events').write_text('high 0\noom_kill 0\n')
    rt.backend.attach(rt.module.MemcgConfig(high_cgroup=str(rt.high),low_cgroups=[str(rt.low)]))
    rt.attached=True
    (rt.parent/'memory.current').write_text(str(int(rt.capacity*.9)))
    rt._poll_backend()
    assert rt.backend.get_stats()['last_trigger']=='usage(90%)'


def test_missing_required_pressure_counter_is_not_treated_as_zero(control):
    rt,_=control
    (rt.high/'memory.events').unlink()
    with pytest.raises(FileNotFoundError):
        rt.start()
