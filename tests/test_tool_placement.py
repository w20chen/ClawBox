from pathlib import Path
from types import SimpleNamespace
import json
import threading

import pytest

from clawbox.cube.client import Ownership, CubeSandboxTcpEndpoint
from clawbox.experiments.tool_placement import (
    ReplicaPlacement, run_four_replica_trial, validate_group_isolation,
)


def fake_topology(root):
    # Two LLC/NUMA domains, four four-core clusters each, two SMT threads/core.
    for cpu in range(64):
        directory = root / f'cpu{cpu}'
        (directory / 'topology').mkdir(parents=True)
        (directory / 'topology/thread_siblings_list').write_text(f'{cpu//2*2}-{cpu//2*2+1}')
        (directory / 'topology/cluster_cpus_list').write_text(f'{cpu//8*8}-{cpu//8*8+7}')
        (directory / f'node{cpu//32}').mkdir()
        cache = directory / 'cache/index3'
        cache.mkdir(parents=True)
        (cache / 'level').write_text('3')
        (cache / 'type').write_text('Unified')
        (cache / 'shared_cpu_list').write_text(f'{cpu//32*32}-{cpu//32*32+31}')


def layout(cpus):
    return tuple(ReplicaPlacement(cpu, cpu//32, index) for index, cpu in enumerate(cpus))


def group(offset):
    return {'packed': layout([offset+x for x in (0, 2, 4, 6)]),
            'spread': layout([offset+x for x in (0, 8, 16, 24)])}


def test_group_isolation_reserves_outer_llc_not_only_cluster(tmp_path):
    fake_topology(tmp_path)
    receipt = validate_group_isolation({'a': group(0), 'b': group(32)}, sysfs=tmp_path)
    assert receipt['a']['packed'][0]['llc'] == list(range(32))
    # Different physical cores/clusters are still insufficient across tools.
    b = {'packed': layout((8, 10, 12, 14)), 'spread': layout((2, 10, 18, 26))}
    with pytest.raises(ValueError, match='outer LLC'):
        validate_group_isolation({'a': group(0), 'b': b}, sysfs=tmp_path)


def test_cluster_isolation_allows_shared_upper_llc_but_rejects_shared_clusters(tmp_path):
    fake_topology(tmp_path)
    for cache in tmp_path.glob('cpu*/cache/index3/shared_cpu_list'):
        cache.write_text('0-63')
    validate_group_isolation({'a': group(0), 'b': group(32)}, sysfs=tmp_path, isolation_domain='cluster')
    with pytest.raises(ValueError, match='shares a cluster'):
        validate_group_isolation({'a': group(0), 'b': group(0)}, sysfs=tmp_path, isolation_domain='cluster')


@pytest.mark.parametrize('cpus, message', [((0, 1, 4, 6), 'physical core'),
                                        ((0, 8, 16, 24), 'share one cluster')])
def test_invalid_packed_layout_is_rejected(tmp_path, cpus, message):
    fake_topology(tmp_path)
    with pytest.raises(ValueError, match=message):
        validate_group_isolation({'a': {**group(0), 'packed': layout(cpus)}}, sysfs=tmp_path)


def test_failed_parallel_restore_preserves_receipt_and_cleans_every_created_vm(tmp_path):
    created, killed = [], []
    placements = layout((0, 2, 4, 6))
    class Client:
        def create_sandbox(self, **kwargs):
            assert kwargs['template'] == 'pre-tool-snapshot'
            identity = kwargs['ownership'].session_id
            if identity.endswith('-r1'):
                raise RuntimeError('restore rejected')
            created.append(identity)
            return identity
        def sandbox_id(self, vm):
            return vm
        def get_tcp_endpoint(self, vm, port):
            return CubeSandboxTcpEndpoint(vm, port, '127.0.0.1:12345')
        def kill_sandbox(self, vm):
            killed.append(vm)
    domain = SimpleNamespace(
        placements={p.domain_index: (str(p.cpu), p.numa_node) for p in placements},
        prepare=lambda *args: None, token=lambda *args: 'token',
        verify=lambda index, role, vm: {'sandbox_id': vm},
    )
    result = run_four_replica_trial(client=Client(), domain=domain,
        snapshot_id='pre-tool-snapshot', placements=placements,
        ownership=Ownership('run', 'attempt', 'owner', 'experiment', 'trial', 'resident'),
        credentials=None, node_name='node', remote_command='true', stdin=b'input',
        execution_id='exec', command_sha256='a'*64, timeout_seconds=5,
        output=tmp_path/'trial', allow_internet_access=True,
        network_allow_out=[], network_deny_out=[])
    assert result['status'] == 'invalid'
    assert 'restore rejected' in result['error']
    assert len(created) == 3 and set(killed) == set(created)
    assert set(result['created_sandbox_ids']) == set(created)
    assert (tmp_path/'trial/stdin.bin').read_bytes() == b'input'
    assert (tmp_path/'trial/trial.json').is_file()


@pytest.mark.parametrize('multiplexed', [False, True])
def test_trial_launches_four_together_and_keeps_raw_invalid_pmu(tmp_path, monkeypatch, multiplexed):
    from clawtune_sidecar.monitoring.pmu import EVENT_SPECS
    from clawbox.experiments import tool_placement
    placements = layout((0, 2, 4, 6))
    created, killed, started = [], [], []
    all_started = threading.Event()
    profile = {'schema': 'pmu_profile_v1', 'execution_id': 'exec', 'source': 'perf_event_open',
        'mode': 'counting', 'scope': 'task-inherit-enable-on-exec', 'llc_semantics_confirmed': True,
        'collector_errors': [], 'coverage': {'status': 'reliable', 'reason': 'execution_exited',
            'eligible_for_kb': True, 'multiplexed': False, 'kernel_included': True,
            'root_and_future_descendants': True, 'running_ratio': 1.0},
        'events': {spec.name: {'supported': True, 'semantics': spec.semantics, 'raw_count': 100,
            'time_enabled_ns': 1000, 'time_running_ns': 500 if multiplexed else 1000,
            'running_ratio': .5 if multiplexed else 1.0, 'error': None} for spec in EVENT_SPECS}}
    class Client:
        def create_sandbox(self, **kwargs):
            identity = kwargs['ownership'].session_id
            created.append(identity)
            return identity
        def sandbox_id(self, vm):
            return vm
        def get_tcp_endpoint(self, vm, port):
            return CubeSandboxTcpEndpoint(vm, port, '127.0.0.1:12345')
        def read_file(self, vm, path):
            if 'pmu-profile' in path:
                return json.dumps(profile)
            if 'tool-bridge' in path:
                return json.dumps({'execution_id': 'exec', 'command_sha256': 'a'*64, 'exit_code': 0}) + '\n'
            return json.dumps({'execution_id': 'exec', 'memory_rss_peak_bytes': 123})
        def kill_sandbox(self, vm):
            killed.append(vm)
    class Process:
        returncode = 0
        def __init__(self, args, **kwargs):
            assert len(created) == 4  # No payload launch before every restore is ready.
            assert args[-1] == 'exact envelope and shell command'
            started.append(args)
            if len(started) == 4:
                all_started.set()
        def communicate(self, data, timeout):
            assert data == b'exact binary\x00input'
            assert all_started.wait(3)  # Serial execution cannot pass this gate.
    monkeypatch.setattr(tool_placement.subprocess, 'Popen', Process)
    domain = SimpleNamespace(
        placements={p.domain_index: (str(p.cpu), p.numa_node) for p in placements},
        prepare=lambda *args: None, token=lambda *args: 'token',
        verify=lambda index, role, vm: {'sandbox_id': vm},
    )
    result = run_four_replica_trial(client=Client(), domain=domain,
        snapshot_id='snapshot', placements=placements,
        ownership=Ownership('run', 'attempt', 'owner', 'experiment', 'trial', 'resident'),
        credentials=SimpleNamespace(client_private='private', host_public='public'),
        node_name='node', remote_command='exact envelope and shell command', stdin=b'exact binary\x00input',
        execution_id='exec', command_sha256='a'*64, timeout_seconds=5,
        output=tmp_path/'trial', allow_internet_access=True,
        network_allow_out=[], network_deny_out=[])
    assert result['status'] == ('invalid' if multiplexed else 'measured')
    assert len(result['replicas']) == 4 and set(killed) == set(created)
    assert result['makespan_seconds'] >= result['host_launch_skew_seconds'] >= 0
    assert len(list((tmp_path/'trial').glob('replica-*/pmu-profile-exec.json'))) == 4
