import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from threading import get_native_id
from types import SimpleNamespace

import pytest

from clawbox.experiments import cpu_qos
from clawbox.experiments.priority import PriorityDomain
from clawbox.experiments.spec import WorkloadSpec, load_workload_cases


@pytest.fixture
def scheduler(monkeypatch):
    policies, writes = {}, []

    def set_policy(tid, policy, param):
        assert param.sched_priority == 0
        writes.append((tid, policy))
        policies[tid] = policy

    api = SimpleNamespace(
        SCHED_OTHER=0, SCHED_IDLE=5,
        sched_setscheduler=set_policy,
        sched_getscheduler=lambda tid: policies.get(tid, 0),
        sched_get_priority_min=lambda policy: 0,
        sched_param=lambda priority: SimpleNamespace(sched_priority=priority),
    )
    monkeypatch.setattr(cpu_qos, 'os', api)
    return api, policies, writes


def test_idle_only_writes_be_and_off_checks_labelled_baseline(scheduler):
    api, policies, writes = scheduler
    threads = [{'tid': 11, 'name': 'vcpu0'}, {'tid': 12, 'name': 'vcpu1'}]
    for policy, qos_class in [('off', 'be'), ('off', 'ls'), ('idle', 'ls')]:
        result = cpu_qos.apply_vcpu_qos(threads, policy=policy, qos_class=qos_class)
        assert all(t['effective_scheduler'] == 'SCHED_OTHER' for t in result['threads'])
    assert not writes
    result = cpu_qos.apply_vcpu_qos(threads, policy='idle', qos_class='be')
    assert writes == [(11, 5), (12, 5)]
    assert all(t['effective_scheduler'] == 'SCHED_IDLE' for t in result['threads'])
    with pytest.raises(RuntimeError, match='CPU QoS mismatch'):
        cpu_qos.apply_vcpu_qos(threads, policy='off', qos_class='be')
    with pytest.raises(RuntimeError, match='CPU QoS mismatch'):
        cpu_qos.apply_vcpu_qos(threads, policy='idle', qos_class='ls')
    assert writes == [(11, 5), (12, 5)]


def test_qos_propagates_set_failure_and_readback_mismatch(scheduler):
    api, policies, writes = scheduler
    threads = [{'tid': 11, 'name': 'vcpu0'}]

    def denied(*args):
        raise PermissionError('permission denied')

    api.sched_setscheduler = denied
    with pytest.raises(RuntimeError, match='host scheduler privileges'):
        cpu_qos.apply_vcpu_qos(threads, policy='idle', qos_class='be')
    api.sched_setscheduler = lambda *args: None
    with pytest.raises(RuntimeError, match='expected SCHED_IDLE'):
        cpu_qos.apply_vcpu_qos(threads, policy='idle', qos_class='be')
    del api.SCHED_IDLE
    with pytest.raises(RuntimeError, match='Linux SCHED_IDLE'):
        cpu_qos.check_cpu_qos_support('idle')


def test_off_neutral_does_not_touch_scheduler(monkeypatch):
    monkeypatch.setattr(cpu_qos, 'os', SimpleNamespace())
    assert cpu_qos.apply_vcpu_qos(
        [{'tid': 11, 'name': 'vcpu0'}], policy='off', qos_class='neutral',
    )['threads'] == []


@pytest.mark.parametrize('group_idle', [False, True])
def test_native_verification_reapplies_qos_to_restored_runtime_and_tool_tids(
        monkeypatch, tmp_path, scheduler, group_idle):
    _, _, writes = scheduler
    mount = tmp_path / 'cgroup'
    parent = mount / 'cube_sandbox/sandbox'
    leaf = parent / '42'
    leaf.mkdir(parents=True)
    registry = tmp_path / 'registry'
    registry.mkdir()
    domain = PriorityDomain(
        parent, concurrency=1, repetition=0, legacy=False,
        placements={0: ('0-1', 0)}, registry=registry, cgroup_mount=mount,
        qos_classes={0: 'be'}, cpu_qos_policy='idle',
        cpu_qos_group_idle=group_idle,
    )
    if group_idle:
        (leaf/'cpu.idle').write_text('0')
        (leaf/'cpu.weight').write_text('100')
        original_write = Path.write_text
        def write(path, text, *args, **kwargs):
            result = original_write(path, text, *args, **kwargs)
            if path.name == 'cpu.idle':
                original_write(path.parent/'cpu.weight', '0' if text == '1' else '100')
            return result
        monkeypatch.setattr(Path, 'write_text', write)
    for name, value in {'cpuset.cpus.effective': '0-1', 'cpuset.mems.effective': '0'}.items():
        (leaf / name).write_text(value)
    for role in ('runtime', 'tool'):
        (registry / domain.token(0, role)).write_text(json.dumps({
            'numa_node': 0, 'path': '/cube_sandbox/sandbox/42',
        }))
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.as_posix().startswith('/proc/') and path.name == 'status':
            name = 'cube-helper' if path.parent.name == '100' else 'vcpu0'
            return f'Name: {name}\nCpus_allowed_list: 0-1\n'
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(os, 'sched_setaffinity', lambda *args: None, raising=False)
    monkeypatch.setattr(os, 'sched_getaffinity', lambda tid: {0, 1}, raising=False)
    monkeypatch.setattr('clawbox.experiments.numa_borrow.read_numa_lru_bytes',
                        lambda path: ({0: 1024}, 0))
    monkeypatch.setattr('clawbox.experiments.numa_borrow.read_numa_anon_bytes',
                        lambda path: {0: 1024})
    for role, tid in [('runtime', 101), ('tool', 201), ('runtime', 301), ('tool', 401)]:
        (leaf / 'cgroup.threads').write_text(f'100 {tid}')
        result = domain.verify(0, role, f'vm-{role}')
        assert result['cpu_qos']['threads'][0]['tid'] == tid
        assert result['cpu_qos']['qos_class'] == 'be'
        if group_idle:
            assert result['cpu_qos']['cgroup']['effective_idle'] == 1
            assert result['cpu_qos']['cgroup']['effective_weight'] == 0
    assert writes == [(101, 5), (201, 5), (301, 5), (401, 5)]
    domain.close()
    if group_idle:
        assert (leaf/'cpu.idle').read_text() == '0'
        assert (leaf/'cpu.weight').read_text() == '100'


@pytest.mark.parametrize('weight', [0, 1])
def test_group_idle_accepts_kernel_weight_representation(tmp_path, weight):
    (tmp_path/'cpu.idle').write_text('1')
    (tmp_path/'cpu.weight').write_text(str(weight))
    result = cpu_qos.apply_group_idle(tmp_path, qos_class='be')
    assert result['effective_idle'] == 1
    assert result['effective_weight'] == weight


def test_group_idle_missing_control_and_bad_readback_fail(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match='CPU controller'):
        cpu_qos.apply_group_idle(tmp_path, qos_class='be')
    (tmp_path/'cpu.idle').write_text('0')
    (tmp_path/'cpu.weight').write_text('100')
    original_write = Path.write_text
    def ignore_policy_write(path, text, *args, **kwargs):
        if path.name == 'cpu.idle':
            return len(text)
        return original_write(path, text, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', ignore_policy_write)
    with pytest.raises(RuntimeError, match='verification failed'):
        cpu_qos.apply_group_idle(tmp_path, qos_class='be')
    with pytest.raises(ValueError, match='explicit'):
        cpu_qos.apply_group_idle(tmp_path, qos_class='neutral')


def test_workload_manifest_preserves_explicit_qos_class(tmp_path):
    manifest = tmp_path / 'workload.json'
    manifest.write_text(json.dumps({'cases': [
        {'case_id': 'foreground', 'qos_class': 'ls', 'replay_trace_reference': 'same.jsonl'},
        {'case_id': 'background', 'qos_class': 'be', 'replay_trace_reference': 'same.jsonl'},
    ]}))
    cases = load_workload_cases(WorkloadSpec(source='synthetic', input=str(manifest)))
    assert [case.qos_class for case in cases] == ['ls', 'be']
    assert cases[0].replay_trace_reference == cases[1].replay_trace_reference
    assert all('qos_class' not in case.metadata for case in cases)


@pytest.fixture
def core_kernel(monkeypatch, scheduler):
    cookies, calls = {}, []
    caller = get_native_id()

    def command(operation, tid=0, result=None):
        source = get_native_id()
        calls.append((operation, source, tid))
        if operation == cpu_qos.PR_SCHED_CORE_CREATE:
            assert tid == 0
            cookies[source] = 1000 + source
        elif operation == cpu_qos.PR_SCHED_CORE_SHARE_TO:
            cookies[tid] = cookies[source]
        else:
            raise AssertionError(operation)

    monkeypatch.setattr(cpu_qos, '_core_command', command)
    monkeypatch.setattr(cpu_qos, 'get_core_cookie', lambda tid=0: cookies.get(tid or get_native_id(), 0))
    return caller, cookies, calls


def test_native_class_anchors_keep_caller_cookie_and_reapply_new_tids(core_kernel):
    caller, cookies, calls = core_kernel
    groups = cpu_qos.CoreSchedulingGroups()
    groups.start()
    anchors = list(groups.anchors.values())
    try:
        results = []
        for qos_class, tid in [('ls', 101), ('be', 201), ('ls', 301), ('be', 401)]:
            results.append(cpu_qos.apply_vcpu_qos(
                [{'tid': tid, 'name': 'vcpu0'}], policy='idle_core', qos_class=qos_class,
                core_groups=groups,
            ))
        assert results[0]['core_cookie'] == results[2]['core_cookie']
        assert results[1]['core_cookie'] == results[3]['core_cookie']
        assert results[0]['core_cookie'] != results[1]['core_cookie']
        assert all(result['core_cookie'] for result in results)
        assert cookies.get(caller, 0) == 0
        assert all(source != caller for _, source, _ in calls)
        assert {target for operation, _, target in calls
                if operation == cpu_qos.PR_SCHED_CORE_SHARE_TO} == {101, 201, 301, 401}
    finally:
        groups.close()
    assert all(not anchor.thread.is_alive() for anchor in anchors)


def test_core_unsupported_and_share_failure_are_not_downgraded(monkeypatch, scheduler):
    def unsupported(tid=0):
        raise OSError(22, 'Invalid argument')

    monkeypatch.setattr(cpu_qos, 'get_core_cookie', unsupported)
    with pytest.raises(RuntimeError, match='CONFIG_SCHED_CORE=y'):
        cpu_qos.CoreSchedulingGroups().start()
    with pytest.raises(RuntimeError, match='active native core groups'):
        cpu_qos.apply_vcpu_qos([{'tid': 11, 'name': 'vcpu0'}],
                                policy='idle_core', qos_class='be')


def test_core_share_failure_and_mismatch_propagate(monkeypatch, core_kernel):
    groups = cpu_qos.CoreSchedulingGroups()
    groups.start()
    original = cpu_qos._core_command
    try:
        def denied(operation, tid=0, result=None):
            if operation == cpu_qos.PR_SCHED_CORE_SHARE_TO:
                raise PermissionError(1, 'Operation not permitted')
            return original(operation, tid, result)

        monkeypatch.setattr(cpu_qos, '_core_command', denied)
        with pytest.raises(RuntimeError, match='host ptrace/scheduler privileges'):
            groups.assign('be', (101,))
        monkeypatch.setattr(cpu_qos, '_core_command', lambda *args: None)
        with pytest.raises(RuntimeError, match='core cookie mismatch'):
            groups.assign('be', (102,))
    finally:
        groups.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='native core scheduling requires Linux')
def test_native_core_selftest_when_supported():
    # An isolated process owns every target and anchor; the test runner never
    # adopts a cookie or SCHED_IDLE, including on assertion failures.
    code = textwrap.dedent('''
        import errno, json, sys
        from threading import Event, Thread
        from clawbox.experiments.cpu_qos import CoreSchedulingGroups, apply_vcpu_qos, get_core_cookie
        try:
            original = get_core_cookie()
        except OSError as error:
            if error.errno in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
                print(str(error))
                sys.exit(77)
            raise
        stop = Event()
        targets = [Thread(target=stop.wait, daemon=True) for _ in range(4)]
        groups = CoreSchedulingGroups()
        try:
            for target in targets:
                target.start()
            groups.start()
            results = []
            for target, qos_class in zip(targets, ('ls', 'be', 'ls', 'be')):
                results.append(apply_vcpu_qos(
                    [{'tid': target.native_id, 'name': 'selftest-vcpu'}],
                    policy='idle_core', qos_class=qos_class, core_groups=groups))
            assert get_core_cookie() == original
            assert results[0]['core_cookie'] == results[2]['core_cookie']
            assert results[1]['core_cookie'] == results[3]['core_cookie']
            assert results[0]['core_cookie'] != results[1]['core_cookie']
            print(json.dumps(results))
        finally:
            stop.set()
            for target in targets:
                target.join(timeout=5)
            groups.close()
    ''')
    result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                            text=True, timeout=45)
    if result.returncode == 77:
        pytest.skip('host kernel lacks PR_SCHED_CORE: ' + result.stdout.strip())
    assert result.returncode == 0, result.stdout + result.stderr


def _write_vcpu_stat(root, tid, *, start, user, system=0):
    folder = root / str(tid) / 'task' / str(tid)
    folder.mkdir(parents=True, exist_ok=True)
    fields = ['0'] * 20
    fields[0], fields[11], fields[12], fields[19] = 'R', str(user), str(system), str(start)
    (folder / 'stat').write_text(f'{tid} (vcpu ) name) ' + ' '.join(fields))


def test_vcpu_usage_exact_class_deltas_and_logical_cpu_denominator(tmp_path):
    now = [0.0]
    sampler = cpu_qos.CpuQosUsageSampler('0-3', proc_root=tmp_path,
        clock=lambda: now[0], clock_ticks_per_second=100)
    for tid in (101, 201, 202):
        _write_vcpu_stat(tmp_path, tid, start=tid, user=10)
        # The root TID view can expose a shared process aggregate. It must
        # never be used in place of the explicit per-task counter below.
        task_stat = tmp_path / str(tid) / 'task' / str(tid) / 'stat'
        (tmp_path / str(tid) / 'stat').write_text(task_stat.read_text())
    sampler.register((0, 'tool'), 'ls', [101])
    sampler.register((1, 'runtime'), 'be', [201])
    sampler.register((1, 'tool'), 'be', [202])
    _write_vcpu_stat(tmp_path, 101, start=101, user=110, system=100)
    for tid in (201, 202):
        _write_vcpu_stat(tmp_path, tid, start=tid, user=110)
    now[0] = 2.0
    result = sampler.sample()
    assert result['allowed_logical_cpus'] == 4
    for item in result['classes'].values():
        assert item['coverage'] == 'complete'
        assert item['observed_cpu_seconds'] == 2.0
        assert item['observed_load_fraction'] == .25
        assert item['cumulative_observed_cpu_seconds'] == 2.0
    assert result['classes']['be']['new_threads'] == 2
    now[0] = 4.0
    result = sampler.sample()
    assert result['classes']['be']['new_threads'] == 0
    assert result['classes']['be']['observed_cpu_seconds'] == 0.0


def test_vcpu_usage_exit_tail_is_partial_and_missing_is_not_zero(tmp_path):
    now = [0.0]
    sampler = cpu_qos.CpuQosUsageSampler('0-3', proc_root=tmp_path,
        clock=lambda: now[0], clock_ticks_per_second=100)
    for tid in (101, 102):
        _write_vcpu_stat(tmp_path, tid, start=tid, user=10)
    sampler.register((0, 'tool'), 'be', [101, 102])
    _write_vcpu_stat(tmp_path, 101, start=101, user=110)
    (tmp_path / '102/task/102/stat').unlink()
    now[0] = 1.0
    be = sampler.sample()['classes']['be']
    assert be['coverage'] == 'partial' and be['lost_exit_tail']
    assert be['departed_threads'] == be['lost_exit_tail_threads'] == 1
    assert be['observed_load_fraction'] is None
    assert be['observed_load_fraction_lower_bound'] == .25
    (tmp_path / '101/task/101/stat').unlink()
    now[0] = 2.0
    be = sampler.sample()['classes']['be']
    assert be['observed_cpu_seconds'] is None
    assert be['observed_load_fraction_lower_bound'] is None
    assert be['cumulative_observed_cpu_seconds'] == 1.0
    assert be['cumulative_coverage'] == 'partial'


def test_vcpu_usage_tid_reuse_never_charges_new_ls_counter_to_be(tmp_path):
    now = [0.0]
    sampler = cpu_qos.CpuQosUsageSampler('0', proc_root=tmp_path,
        clock=lambda: now[0], clock_ticks_per_second=100)
    _write_vcpu_stat(tmp_path, 101, start=100, user=10)
    sampler.register((0, 'tool'), 'be', [101])
    _write_vcpu_stat(tmp_path, 101, start=100, user=110)
    now[0] = 1.0
    assert sampler.sample()['classes']['be']['observed_cpu_seconds'] == 1.0
    _write_vcpu_stat(tmp_path, 101, start=200, user=5000)
    sampler.register((1, 'runtime'), 'ls', [101])
    _write_vcpu_stat(tmp_path, 101, start=200, user=5010)
    now[0] = 2.0
    result = sampler.sample()['classes']
    assert result['be']['observed_cpu_seconds'] is None
    assert result['be']['cumulative_observed_cpu_seconds'] == 1.0
    assert result['be']['lost_exit_tail']
    assert result['ls']['observed_cpu_seconds'] == .1
    assert result['ls']['observed_load_fraction'] == .1


def test_vcpu_registration_during_proc_io_does_not_claim_aligned_load(tmp_path, monkeypatch):
    now = [0.0]
    sampler = cpu_qos.CpuQosUsageSampler('0-1', proc_root=tmp_path,
        clock=lambda: now[0], clock_ticks_per_second=100)
    for tid in (101, 202):
        _write_vcpu_stat(tmp_path, tid, start=tid, user=10)
    sampler.register((0, 'tool'), 'ls', [101])
    read = sampler._read
    registered = []

    def interleaved_read(tid):
        if tid == 101 and not registered:
            registered.append(True)
            sampler.register((1, 'tool'), 'be', [202])
        return read(tid)

    monkeypatch.setattr(sampler, '_read', interleaved_read)
    now[0] = 1.0
    first = sampler.sample()['classes']['be']
    assert first['coverage'] == 'partial' and first['observed_cpu_seconds'] is None
    _write_vcpu_stat(tmp_path, 202, start=202, user=110)
    now[0] = 2.0
    second = sampler.sample()['classes']['be']
    assert second['coverage'] == 'partial' and second['observed_load_fraction'] is None
    assert second['observed_load_fraction_lower_bound'] is None
    assert second['cumulative_observed_cpu_seconds'] == 1.0
    now[0] = 3.0
    assert sampler.sample()['classes']['be']['coverage'] == 'complete'
