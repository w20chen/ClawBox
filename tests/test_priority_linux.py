"""Opt-in kernel tests on an idle dedicated Cube host.

CLAWBOX_TEST_CGROUP=/sys/fs/cgroup/cube_sandbox/sandbox sudo -E ... pytest ...
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

pytestmark=pytest.mark.skipif(
    sys.platform!='linux' or not os.environ.get('CLAWBOX_TEST_CGROUP')
    or getattr(os,'geteuid',lambda:1)()!=0,
    reason='requires explicit idle Cube cgroup and Linux root')


@pytest.fixture
def parent():
    from clawbox.experiments.priority import PriorityDomain
    base=Path(os.environ['CLAWBOX_TEST_CGROUP']).resolve()
    assert base.is_relative_to(Path('/sys/fs/cgroup/cube_sandbox/sandbox'))
    assert 'populated 0' in (base/'cgroup.events').read_text()
    PriorityDomain._enable(base)
    root=base/('clawbox-test-'+uuid.uuid4().hex)
    root.mkdir()
    (root/'memory.max').write_text(str(384*1024**2))
    (root/'memory.swap.max').write_text('0')
    try:
        yield root
    finally:
        root.rmdir()


def test_supervisor_cleans_groups_and_capabilities_after_worker_exit(parent,tmp_path):
    from clawbox.experiments.priority import cleanup_priority_domains,REGISTRY
    script='''
import os,sys
from pathlib import Path
from clawbox.experiments.priority import PriorityDomain
d=PriorityDomain(Path(sys.argv[1]),concurrency=2,repetition=0,journal_directory=Path(sys.argv[2]))
d.start()
(d.high/'memory.low').write_text('33554432')
os._exit(0)
'''
    subprocess.run([sys.executable,'-c',script,str(parent),str(tmp_path)],check=True)
    journals=list(tmp_path.glob('clawbox-*.json'))
    assert len(journals)==1
    record=json.loads(journals[0].read_text())
    assert all((REGISTRY/token).exists() for token in record['tokens'])
    cleanup_priority_domains(tmp_path)
    assert not list(parent.glob('clawbox-*'))
    assert not list(tmp_path.glob('clawbox-*.json'))
    assert not any((REGISTRY/token).exists() for token in record['tokens'])


def test_real_kernel_upstream_activation_and_low_throttling(parent):
    from clawbox.experiments.runtime_qualification import kernel_control_probe
    result=kernel_control_probe(parent)
    assert result['activation_verified'] and result['low_throttling_verified']
    assert result['oom_kill']==0


def test_cgroup_oom_is_counted_even_with_runtime_control_off(parent):
    from clawbox.experiments.congestion import RuntimeMemoryControl
    from clawbox.experiments.priority import PriorityDomain
    from clawbox.experiments.worker import classify_arm_failure
    from clawbox.experiments.results import FailureCategory
    domain = PriorityDomain(parent, concurrency=2, repetition=0)
    domain.start()
    control = RuntimeMemoryControl(parent, high=domain.high, low=domain.low, enabled=False)
    program = '''
import mmap, os, pathlib, sys
pathlib.Path(sys.argv[1], 'cgroup.procs').write_text(str(os.getpid()))
data=mmap.mmap(-1,512*1024*1024)
for i in range(0,len(data),4096): data[i]=1
'''
    proc = subprocess.Popen([sys.executable, '-c', program, str(domain.leaves[1, 'tool'])])
    try:
        assert proc.wait(timeout=15) == -9
        control.close()
        assert control.stats['oom_kill_events'] == 1
        assert control.stats['polls'] == 0
        assert classify_arm_failure(RuntimeError('CubeSandbox RPC disconnected'),
            domain_oom_kills=control.stats['oom_kill_events']) is FailureCategory.OOM
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        control.close()
        domain.close()


def test_psi_admission_uses_real_stalls_and_reopens_after_pressure_clears(parent, tmp_path):
    import time
    from clawbox.experiments.admission import DemandLedger
    from clawbox.experiments.baselines import resolve_baseline
    from clawbox.experiments.policy import PolicyCoordinator, AdmissionTimeout
    from clawbox.experiments.priority import PriorityDomain

    if not (parent/'memory.pressure').exists():
        pytest.skip('kernel has no PSI; reclaim-delta fallback has separate coverage')
    mib = 1024**2
    domain = PriorityDomain(parent, concurrency=2, repetition=0)
    domain.start()
    marker = tmp_path/'allocated'
    program = '''
import mmap, os, pathlib, sys, time
pathlib.Path(sys.argv[1], 'cgroup.procs').write_text(str(os.getpid()))
held=mmap.mmap(-1,348*1024*1024)
for i in range(0,len(held),4096): held[i]=1
pathlib.Path(sys.argv[2]).touch()
while True:
    extra=mmap.mmap(-1,1024*1024)
    for i in range(0,len(extra),4096): extra[i]=1
    extra.close()
    time.sleep(.01)
'''
    proc = subprocess.Popen([sys.executable, '-c', program,
        str(domain.leaves[1, 'tool']), str(marker)], stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic()+10
        while not marker.exists() and time.monotonic()<deadline:
            assert proc.poll() is None
            time.sleep(.02)
        assert marker.exists()
        ledger = DemandLedger(sample=lambda: (int((parent/'memory.current').read_text()), 1024*mib),
                              capacity=384*mib, cgroup=parent)
        with ledger.condition:
            ledger.reasons(0)  # establish a pressure-delta baseline
        (domain.low/'memory.high').write_text(str(256*mib))
        deadline = time.monotonic()+5
        reasons = ()
        while time.monotonic()<deadline:
            with ledger.condition:
                reasons = ledger.reasons(0)
            if 'sustained_memory_congestion' in reasons:
                break
            time.sleep(.12)
        assert 'sustained_memory_congestion' in reasons
        coordinators = [PolicyCoordinator(resolve_baseline(name).as_policy(), budget_mib=384,
            emergency_free_mib=0, operation_headroom_mib=0, demand_ledger=ledger)
            for name in ('runtime-resident', 'predicted-resident')]
        coordinators[0].acquire('off', 0, .05)
        with pytest.raises(AdmissionTimeout):
            coordinators[1].acquire('predicted', 0, .05)
        (domain.low/'memory.high').write_text('max')
        proc.terminate()
        proc.wait(timeout=5)
        coordinators[1].acquire('recovered', 1, 2)
        coordinators[1].release('recovered', 1)
        assert 'oom_kill 0' in (parent/'memory.events').read_text().splitlines()
    finally:
        (domain.low/'memory.high').write_text('max')
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        proc.stderr.close()
        domain.close()
