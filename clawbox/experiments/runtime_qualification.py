"""Bounded real-kernel and Cube placement probes; no performance claims."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import shlex

from .congestion import MIB, RuntimeMemoryControl
from .priority import PriorityDomain


def kernel_control_probe(parent: Path, *, source: Path | None = None,
                         control_options: dict | None = None) -> dict:
    """Exercise upstream activation and LOW throttling in an isolated 384 MiB domain.

    These are host allocation processes, not guest allocations. This proves
    kernel control operation, not that freeing guest pages reduces host RSS.
    """
    parent=parent.resolve()
    if not parent.is_relative_to(Path('/sys/fs/cgroup/cube_sandbox/sandbox')):
        raise ValueError('kernel probe requires the Cube VM cgroup subtree')
    root = parent / ('clawbox-probe-' + uuid.uuid4().hex)
    PriorityDomain._enable(parent)
    root.mkdir()
    domain = PriorityDomain(root, concurrency=2, repetition=0)
    control = None
    processes = []
    events = []
    result = None
    try:
        (root/'memory.max').write_text(str(384*MIB))
        (root/'memory.swap.max').write_text('0')
        domain.start()
        options = dict(control_options or {})
        options.setdefault('interval', .02)
        control = RuntimeMemoryControl(root, source, high=domain.high, low=domain.low,
                                       emit=events.append, **options)
        control.start()
        # Upstream signal 1: charge HIGH above its soft limit. Hard limits stay
        # unchanged, and total requested allocation remains below the probe cap.
        (domain.high/'memory.high').write_text(str(32*MIB))
        program = """
import mmap, pathlib, sys, time
pathlib.Path(sys.argv[1], 'cgroup.procs').write_text(str(__import__('os').getpid()))
n=int(sys.argv[2])*1024*1024
buf=mmap.mmap(-1,n)
for i in range(0,n,4096): buf[i]=1
deadline=time.monotonic()+10
while time.monotonic()<deadline:
    extra=mmap.mmap(-1,1024*1024)
    for i in range(0,len(extra),4096): extra[i]=1
    extra.close()
    time.sleep(.01)
"""
        for index, mib in ((1,240),(0,48)):
            processes.append(subprocess.Popen([sys.executable, '-c', program,
                str(domain.leaves[index,'tool']), str(mib)], stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE))
        deadline = time.monotonic()+8
        activated = throttled = False
        while time.monotonic()<deadline:
            control.check()
            activated = any(e.get('activations',0)>0 for e in events)
            low_events = control.module._read_memory_events(str(domain.low))
            throttled = low_events.get('high',0)>0
            if activated and throttled:
                break
            for proc in processes:
                if proc.poll() is not None and proc.returncode:
                    raise RuntimeError('kernel allocation probe failed: '+proc.stderr.read().decode())
            time.sleep(.02)
        (domain.high/'memory.high').write_text('max')
        for proc in processes:
            proc.terminate()
            proc.wait(timeout=5)
        oom = control.module._read_memory_events(str(root)).get('oom_kill',0)
        if not activated or not throttled or oom:
            raise RuntimeError(f'kernel control qualification failed: activation={activated}, LOW throttle={throttled}, oom_kill={oom}')
        result = {'passed': True, 'scope': 'host_allocators_in_isolated_cgroup',
                  'activation_verified': activated, 'low_throttling_verified': throttled,
                  'oom_kill': oom, 'events': events}
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            if proc.stderr:
                proc.stderr.close()
        try:
            if control is not None:
                control.close()
        finally:
            domain.close()
            root.rmdir()
    return result


def guest_pressure_probe(domain, lifecycles, *, source=None, control_options=None):
    """Exercise default upstream signals using real guest allocation growth.

    Only this probe's private parent budget is changed. No artificial HIGH
    memory.high event is injected and no host-wide limit is modified.
    """
    from clawbox.cube import CubeSandboxClient

    client = CubeSandboxClient()
    parent = domain.parent
    before = int((parent/'memory.current').read_text())
    high_before = int((domain.high/'memory.current').read_text())
    low_before = int((domain.low/'memory.current').read_text())
    # Full-copy restore may already charge several GiB to each VM. LOW must
    # exceed half of the measured parent budget for its soft limit to bind.
    # Size the asymmetric test load using those observations, not VM RAM size.
    amounts = (64*MIB, max(512*MIB, int(1.35*(high_before+64*MIB)-low_before)+128*MIB))
    outer_limit = (parent.parent/'memory.max').read_text().strip()
    events = []
    control = None
    processes = []
    closed = False
    try:
        for index in (1, 0):
            vm = lifecycles[index*2 + 1].sandbox
            available = client.run_command(vm, "awk '/MemAvailable:/ {print $2}' /proc/meminfo", timeout_s=10)
            if available.exit_code or int(available.stdout.strip())*1024 < amounts[index]*2:
                raise RuntimeError('insufficient guest headroom for the VM pressure probe')
            marker = '/tmp/clawbox-pressure-' + uuid.uuid4().hex
            program = ('import mmap,time; '
                       f'b=mmap.mmap(-1,{amounts[index]}); '
                       'exec("for i in range(0,len(b),4096): b[i]=1"); '
                       f'open({marker!r},"w").write(str(len(b))); '
                       'exec("end=time.monotonic()+120\\nwhile time.monotonic()<end:\\n '
                       'extra=mmap.mmap(-1,4*1024*1024)\\n '
                       'for i in range(0,len(extra),4096): extra[i]=1\\n '
                       'extra.close()\\n time.sleep(.05)")')
            result = client.run_command(vm, 'nohup python3 -c '+shlex.quote(program)+
                ' >/tmp/clawbox-pressure-probe.log 2>&1 & echo $!', timeout_s=10)
            if result.exit_code or not result.stdout.strip().isdigit():
                raise RuntimeError('cannot start guest pressure probe')
            processes.append((vm, int(result.stdout.strip()), amounts[index], marker))
        # A guest allocation is not a host-charge estimate: COW faults, guest
        # page tables and VMM work also consume memory. Finish the allocations
        # under the original host budget, then size this private pressure domain
        # from measured residency. The small continuing faults exercise LOW.
        for vm, pid, amount, marker in processes:
            proof = client.run_command(vm,
                f'for i in $(seq 1 200); do kill -0 {pid} || exit 1; '
                f'if test -f {shlex.quote(marker)}; then cat {shlex.quote(marker)}; exit; fi; '
                'sleep .1; done; exit 1', timeout_s=30)
            if proof.exit_code or proof.stdout.strip() != str(amount):
                raise RuntimeError('guest pressure allocation did not complete before applying the probe budget')
        samples = []
        for _ in range(10):
            samples.append(int((parent/'memory.current').read_text()))
            time.sleep(.1)
        charged = max(samples)
        budget_observations = []
        for attempt in range(2):
            cap = max(int(charged/.90), charged + 64*MIB)
            if outer_limit != 'max' and cap > int(outer_limit):
                raise RuntimeError('VM control probe does not fit within the configured parent budget')
            (parent/'memory.max').write_text(str(cap))
            cap = int((parent/'memory.max').read_text())
            control = RuntimeMemoryControl(
                parent, source, high=domain.high, low=domain.low,
                emit=events.append, **dict(control_options or {}),
            )
            closed = False
            control.start()
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                control.check()
                high = control.module._read_memory_events(str(domain.low)).get('high', 0)
                if control.stats.get('upstream', {}).get('activations', 0) and high:
                    break
                time.sleep(.1)
            stats = control.backend.get_stats()
            throttled = control.module._read_memory_events(str(domain.low)).get('high', 0) > 0
            oom = control.module._read_memory_events(str(parent)).get('oom_kill', 0)
            current = int((parent/'memory.current').read_text())
            budget_observations.append({'charged_bytes': charged, 'capacity_bytes': cap,
                                        'current_bytes': current, 'activation_verified': stats['activations'] > 0,
                                        'low_throttling_verified': throttled, 'oom_kill': oom})
            if throttled or oom or attempt or not stats['activations'] or current >= charged * .8:
                break
            # Startup/reclaimable charges can disappear after activation, so a
            # budget derived from them no longer exercises LOW. Re-measure once
            # with the same guest allocations alive. Only this private probe's
            # budget changes; activation, real throttling and no OOM stay required.
            control.close()
            closed = True
            (parent/'memory.max').write_text(outer_limit)
            samples = []
            for _ in range(10):
                samples.append(int((parent/'memory.current').read_text()))
                time.sleep(.1)
            charged = max(samples)
        peak = max([before] + [row['charged_bytes'] for row in budget_observations]
                   + [e['parent_current_bytes'] for e in events
                               if 'parent_current_bytes' in e])
        current = int((parent/'memory.current').read_text())
        # LOW includes the VM's envd as well as its workload. Do not require an
        # RPC to complete while deliberately throttling that whole VM. Capture
        # host evidence first, restore the limits, then verify forward progress.
        control.close()
        closed = True
        (parent/'memory.max').write_text(outer_limit)
        guest_evidence = []
        for vm, pid, amount, marker in processes:
            proof = client.run_command(vm,
                f'for i in $(seq 1 100); do kill -0 {pid} || exit 1; '
                f'if test -f {shlex.quote(marker)}; then cat {shlex.quote(marker)}; exit; fi; '
                'sleep .1; done; exit 1', timeout_s=20)
            guest_evidence.append({'requested_bytes': amount,
                'allocation_completed': proof.exit_code == 0 and proof.stdout.strip() == str(amount)})
        result = {'scope': 'guest_allocations', 'activation_verified': stats['activations'] > 0,
                  'low_throttling_verified': throttled, 'oom_kill': oom,
                  'parent_capacity_bytes': cap, 'baseline_bytes': before,
                  'charged_bytes_before_control': charged,
                  'budget_basis': 'measured_host_charge_after_guest_allocation',
                  'budget_observations': budget_observations,
                  'observed_peak_bytes': peak, 'guest_allocations': guest_evidence,
                  'current_bytes': current, 'events': events,
                  'completion_checked_after_control_release': True}
        result['passed'] = (result['activation_verified'] and throttled and not oom
                            and peak > before and len(guest_evidence) == 2
                            and all(p['allocation_completed'] for p in guest_evidence))
        return result
    finally:
        try:
            if control is not None and not closed:
                control.close()
        finally:
            (parent/'memory.max').write_text(outer_limit)
            for vm, pid, _, marker in processes:
                client.run_command(vm, f'kill {pid}; rm -f {shlex.quote(marker)}', timeout_s=10)


def runtime_roundtrip_test(spec, *, artifact_root: Path) -> dict:
    """Verify both VM roles, priorities and configured snapshot restores."""
    from clawbox.cube import CubeSandboxClient, CubeSandboxLifecycle, OwnedSandboxJournal, Ownership
    from .snapshot_pool import WarmSnapshotPool
    from .spec_types import ReclamationPolicy

    if not spec.resources.local_memory_cgroup:
        return {'required': False, 'passed': True}
    artifact_root.mkdir(parents=True, exist_ok=False)
    source = Path(spec.resources.agentcgroup_source) if spec.resources.agentcgroup_source else None
    control_options = {
        'interval': spec.resources.agentcgroup_poll_interval_seconds,
        'event_threshold': spec.resources.agentcgroup_event_threshold,
        'pressure_ratio': spec.resources.agentcgroup_pressure_ratio,
        'protection_window_seconds': (
            spec.resources.agentcgroup_protection_window_seconds
        ),
    }
    kernel = kernel_control_probe(
        Path(spec.resources.local_memory_cgroup), source=source,
        control_options=control_options,
    )
    (artifact_root/'kernel-control.json').write_text(json.dumps(kernel, indent=2))
    probe_id = 'qualification-priority-' + uuid.uuid4().hex[:12]
    client = CubeSandboxClient(journal=OwnedSandboxJournal(artifact_root/'owned-sandboxes.jsonl'))
    parent = Path(spec.resources.local_memory_cgroup)
    probe_root = parent / ('clawbox-vm-probe-' + uuid.uuid4().hex)
    domain = PriorityDomain(probe_root, concurrency=2, repetition=0)
    lifecycles = []
    placements = []
    observations = []
    primary = None
    cleanup = []
    vm_control = None
    snapshots = any(p.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE for p in spec.policies)
    pool = WarmSnapshotPool(spec.resources.warm_memory_capacity_mib*MIB,
        transfer_concurrency=spec.resources.pool_transfer_concurrency,
        transfer_timeout_seconds=spec.resources.pool_transfer_timeout_seconds) if spec.resources.warm_memory_capacity_mib else None
    try:
        PriorityDomain._enable(parent)
        probe_root.mkdir()
        (probe_root/'memory.max').write_text((parent/'memory.max').read_text())
        (probe_root/'memory.swap.max').write_text('0')
        domain.start()
        for index in (0,1):
            for role in ('runtime','tool'):
                vm = spec.runtime if role=='runtime' else spec.sandbox
                def verify(sandbox_id, index=index, role=role):
                    placements.append(domain.verify(index,role,sandbox_id))
                lifecycle = CubeSandboxLifecycle(client, template=vm.template,
                    node_name=spec.resources.target_node,
                    ownership=Ownership(probe_id,'runtime-roundtrip',probe_id,spec.experiment_id,
                                        f'{index}-{role}','qualification-priority'),
                    role=role, allow_internet_access=vm.allow_internet_access,
                    cgroup_token=domain.token(index,role), verify_cgroup=verify,
                    prepare_cgroup=lambda index=index,role=role: domain.prepare(index,role),
                    warm_snapshot_root=spec.resources.warm_snapshot_root,
                    cold_snapshot_root=spec.resources.cold_snapshot_root,
                    snapshot_mechanism=spec.resources.snapshot_mechanism,
                    snapshot_storage=spec.resources.snapshot_storage, snapshot_pool=pool,
                    default_snapshot_tier=spec.resources.default_snapshot_tier,
                    snapshot_reservation_bytes=(vm.memory_mib+256)*MIB)
                lifecycles.append(lifecycle)
                lifecycle.start()
                leaf=domain.leaves[index,role]
                observations.append({'phase':'created', 'role':role,'priority':domain.priorities[index],
                    'current_bytes':int((leaf/'memory.current').read_text()),
                    'memory_stat':(leaf/'memory.stat').read_text()})
                if snapshots:
                    lifecycle.sandbox.files.write('/tmp/clawbox-priority-marker',probe_id)
                    lifecycle.checkpoint_and_evict()
                    lifecycle.restore()
                    if lifecycle.sandbox.files.read('/tmp/clawbox-priority-marker')!=probe_id:
                        raise RuntimeError('priority probe lost guest state across restore')
        vm_control = guest_pressure_probe(
            domain, lifecycles, source=source, control_options=control_options,
        )
        (artifact_root/'vm-control.json').write_text(json.dumps(vm_control, indent=2))
        if not vm_control['passed']:
            raise RuntimeError('default AgentCgroup control was not verified on guest allocations')
    except BaseException as exc:
        primary=exc
    finally:
        for lifecycle in reversed(lifecycles):
            try:
                lifecycle.close()
            except Exception as exc:
                cleanup.append(str(exc))
        try:
            client.kill_owned_sandboxes(probe_id)
            if client.list_owned_sandboxes(probe_id):
                raise RuntimeError('priority probe left owned VMs')
        except Exception as exc:
            cleanup.append(str(exc))
        try:
            domain.close()
            probe_root.rmdir()
        except Exception as exc:
            cleanup.append(str(exc))
    result={'required':True,'passed':primary is None and not cleanup,
            'kernel_control': {k:v for k,v in kernel.items() if k!='events'},
            'vm_control': {k:v for k,v in (vm_control or {}).items() if k!='events'},
            'placements':placements,'host_memory_observations':observations,
            'restore_verified':snapshots and primary is None,
            'snapshot_mechanism':spec.resources.snapshot_mechanism,
            'guest_free_releases_host_memory':'not_established_by_this_probe',
            'failure':str(primary) if primary else None,'cleanup_errors':cleanup}
    (artifact_root/'result.json').write_text(json.dumps(result,indent=2))
    if primary:
        if cleanup: primary.add_note('; '.join(cleanup))
        raise primary
    if cleanup:
        raise RuntimeError('priority probe cleanup failed: '+'; '.join(cleanup))
    return result
