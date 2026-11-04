"""Four native Cube VM replicas of one immutable pre-tool snapshot.

The experiment worker owns prefix replay, snapshot creation, target selection
and LLC reservations. This module runs one trial; it never substitutes a fresh
task image for a pre-tool snapshot or retries a payload that might have run.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, replace
import hashlib
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import shlex
import uuid
from typing import Any

from .openclaw_driver import native_ssh_route
from .topology import parse_cpu_list


RUNTIME_LAUNCHER = r'''
import base64, concurrent.futures, json, pathlib, subprocess, sys, threading, time
root=pathlib.Path(sys.argv[1]); config=json.loads((root/'input.json').read_text())
release={}; barrier=threading.Barrier(5, action=lambda: release.update(monotonic_ns=time.monotonic_ns(),wall_ns=time.time_ns()))
def command(replica):
    return [config['real_ssh'],'-i',str(root/'key'),'-o','UserKnownHostsFile='+str(root/'known_hosts'),
        '-o','StrictHostKeyChecking=yes','-o','UpdateHostkeys=no','-o','HostKeyAlias=clawbox-placement',
        '-o','BatchMode=yes','-o','ControlMaster=no','-o','ControlPath=none','-o','ConnectTimeout=5',
        '-p',str(replica['port']),'executor@'+replica['host']]
def prepare(replica):
    deadline=time.monotonic()+60
    while True:
        ready=subprocess.run(command(replica)+['true'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        if ready.returncode==0: return
        if ready.returncode!=255 or time.monotonic()>=deadline:
            raise RuntimeError('SSH readiness failed: '+ready.stderr.decode(errors='replace')[-1000:])
        time.sleep(.25)
def execute(replica):
    index=replica['index']; target=root/str(index); target.mkdir()
    with (target/'stdout.bin').open('wb') as out,(target/'stderr.bin').open('wb') as err:
        barrier.wait(timeout=60)
        launched=time.monotonic_ns()
        child=subprocess.Popen(command(replica)+[config['remote_command']],stdin=subprocess.PIPE,stdout=out,stderr=err)
        data=base64.b64decode(config['stdin_b64']); timed_out=False
        sender=None
        try:
            if config['stdin_eof']:
                child.communicate(data,timeout=config['timeout_seconds']+30)
            else:
                def send():
                    try: child.stdin.write(data); child.stdin.flush()
                    except BrokenPipeError: pass
                sender=threading.Thread(target=send,daemon=True); sender.start()
                child.wait(timeout=config['timeout_seconds']+30)
        except subprocess.TimeoutExpired:
            timed_out=True; child.kill(); child.wait()
        ended=time.monotonic_ns()
        if sender: sender.join(timeout=5)
        if child.stdin and not child.stdin.closed: child.stdin.close()
    return {'index':index,'controller_launch_monotonic_ns':launched,'controller_reaped_monotonic_ns':ended,
        'ssh_exit_code':child.returncode,'transport_timed_out':timed_out,
        'stdout_b64':base64.b64encode((target/'stdout.bin').read_bytes()).decode(),
        'stderr_b64':base64.b64encode((target/'stderr.bin').read_bytes()).decode()}
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(prepare,config['replicas']))
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    futures=[pool.submit(execute,r) for r in config['replicas']]
    barrier.wait(timeout=60)
    rows=[f.result() for f in futures]
(root/'result.json').write_text(json.dumps({'release':release,'replicas':rows}))
'''


@dataclass(frozen=True)
class ReplicaPlacement:
    cpu: int
    numa_node: int
    domain_index: int


def inspect_cpu(cpu: int, *, sysfs: Path = Path('/sys/devices/system/cpu')) -> dict:
    root = sysfs / f'cpu{cpu}'
    siblings = parse_cpu_list((root / 'topology/thread_siblings_list').read_text())
    cluster = parse_cpu_list((root / 'topology/cluster_cpus_list').read_text())
    caches = []
    for index in (root / 'cache').glob('index*'):
        if (index / 'type').read_text().strip() in {'Unified', 'Data'}:
            caches.append((int((index / 'level').read_text()),
                           tuple(sorted(parse_cpu_list((index / 'shared_cpu_list').read_text())))))
    if not caches:
        raise ValueError(f'CPU {cpu}: no data/unified cache topology')
    llc = set(max(caches)[1])
    nodes = [int(p.name[4:]) for p in root.glob('node[0-9]*')]
    if len(nodes) != 1 or cpu not in siblings or not siblings <= cluster <= llc:
        raise ValueError(f'CPU {cpu}: inconsistent physical-core/cluster/LLC/NUMA topology')
    return {'cpu': cpu, 'numa_node': nodes[0], 'siblings': sorted(siblings),
            'cluster': sorted(cluster), 'llc': sorted(llc)}


def validate_group_isolation(groups: dict[str, dict[str, tuple[ReplicaPlacement, ...]]],
                             *, sysfs: Path = Path('/sys/devices/system/cpu'), isolation_domain='llc') -> dict:
    """Reject SMT sharing, incorrect layout labels and cross-tool LLC overlap."""
    receipts, occupied = {}, set()
    for group, layouts in groups.items():
        if not {'packed', 'spread'} <= layouts.keys():
            raise ValueError(f'{group}: packed and spread layouts are required')
        group_llcs = set()
        receipts[group] = {}
        for name, replicas in layouts.items():
            if len(replicas) != 4:
                raise ValueError(f'{group}/{name}: expected four replicas')
            cores, clusters, receipt = set(), set(), []
            for replica in replicas:
                info = inspect_cpu(replica.cpu, sysfs=sysfs)
                if info['numa_node'] != replica.numa_node:
                    raise ValueError(f'{group}/{name}: CPU and requested memory NUMA differ')
                core = tuple(info['siblings'])
                if core in cores:
                    raise ValueError(f'{group}/{name}: replicas share a physical core')
                cores.add(core)
                clusters.add(tuple(info['cluster']))
                group_llcs.add(tuple(info[isolation_domain]))
                receipt.append(info)
            if name == 'packed' and len(clusters) != 1:
                raise ValueError(f'{group}: packed replicas must share one cluster')
            if name in {'spread', 'within_numa'} and len(clusters) != 4:
                raise ValueError(f'{group}/{name}: replicas must occupy four clusters')
            if name == 'within_numa' and len({r['numa_node'] for r in receipt}) != 1:
                raise ValueError(f'{group}: within_numa layout crosses NUMA nodes')
            receipts[group][name] = receipt
        if occupied & group_llcs:
            raise ValueError(f'{group}: another tool group shares an outer LLC domain' if isolation_domain == 'llc'
                             else f'{group}: another tool group shares a cluster')
        occupied.update(group_llcs)
    return receipts


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding='utf-8')


def run_four_replica_trial(*, client: Any, domain: Any, snapshot_id: str,
                           placements: tuple[ReplicaPlacement, ...], ownership: Any,
                           credentials: Any, node_name: str, remote_command: str,
                           stdin: bytes, execution_id: str, command_sha256: str,
                           timeout_seconds: float, output: Path,
                           allow_internet_access: bool, network_allow_out: list[str],
                           network_deny_out: list[str], stdin_eof: bool = True,
                           verify_state=None, runtime_vm=None, real_ssh='/usr/bin/ssh', create_gate=None) -> dict:
    """Restore four replicas, release SSH launches together and preserve evidence.

    Host monotonic release-to-last-reap time is the primary makespan. It includes
    SSH command dispatch and collector finalization, but excludes VM startup,
    snapshot restore, artifact copying and VM cleanup. Guest execution times and
    launch skew are retained separately; callers must qualify synchronization on
    the actual host before interpreting short-call sensitivities.
    """
    if len(placements) != 4 or len({p.cpu for p in placements}) != 4:
        raise ValueError('a trial requires four distinct physical-core placements')
    if not snapshot_id or not execution_id or timeout_seconds <= 0:
        raise ValueError('snapshot, execution identity and deadline are required')
    for p in placements:
        if domain.placements.get(p.domain_index) != (str(p.cpu), p.numa_node):
            raise ValueError('trial placement was not registered with the worker domain')
    output.mkdir(parents=True, exist_ok=False)
    # The complete command includes the original bridge envelope and shell
    # launcher, not just the human-readable logical command.
    (output / 'remote-command.txt').write_text(remote_command, encoding='utf-8')
    (output / 'stdin.bin').write_bytes(stdin)
    record = {'snapshot_id': snapshot_id, 'execution_id': execution_id,
              'profile_command_sha256': command_sha256,
              'remote_command_sha256': hashlib.sha256(remote_command.encode()).hexdigest(),
              'stdin_sha256': hashlib.sha256(stdin).hexdigest(), 'stdin_bytes': len(stdin),
              'stdin_eof': stdin_eof,
              'placements': [vars(p) for p in placements], 'status': 'preparing',
              'timing_definition': 'host monotonic common SSH launch release to last SSH process reap',
              'replicas': [], 'cleanup_errors': []}
    _write_json(output / 'trial.json', record)
    live: list[Any] = []
    release: dict[str, int] = {}
    barrier = threading.Barrier(5, action=lambda: release.update(monotonic_ns=time.monotonic_ns(), wall_ns=time.time_ns()))

    def create(replica_index: int):
        p = placements[replica_index]
        domain.prepare(p.domain_index, 'tool')
        vm_ownership = replace(ownership, session_id=f'{ownership.session_id}-r{replica_index}')
        with create_gate if create_gate is not None else nullcontext():
            vm = client.create_sandbox(template=snapshot_id, node_name=node_name,
                ownership=vm_ownership, env_vars={'CLAWBOX_CGROUP_TOKEN': domain.token(p.domain_index, 'tool')},
                allow_internet_access=allow_internet_access,
                network_allow_out=network_allow_out, network_deny_out=network_deny_out)
        live.append(vm)
        evidence = domain.verify(p.domain_index, 'tool', client.sandbox_id(vm))
        route = native_ssh_route(client.get_tcp_endpoint(vm, 2222), epoch=1)
        folder = output / f'replica-{replica_index}'
        folder.mkdir()
        _write_json(folder / 'placement.json', evidence)
        if verify_state is not None:
            _write_json(folder / 'restored-state.json', verify_state(vm))
        return vm, route, folder

    def execute(prepared, key: Path, known_hosts: Path):
        vm, route, folder = prepared
        args = ['ssh', '-i', str(key), '-o', f'UserKnownHostsFile={known_hosts}',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostkeys=no',
                '-o', 'HostKeyAlias=clawbox-placement', '-o', 'BatchMode=yes',
                '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                '-o', 'ConnectTimeout=15', '-p', str(route.port),
                f'executor@{route.host}', remote_command]
        barrier.wait(timeout=60)
        launched = time.monotonic_ns()
        timed_out = False
        with (folder / 'stdout.bin').open('wb') as stdout, (folder / 'stderr.bin').open('wb') as stderr:
            child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
            try:
                if stdin_eof:
                    child.communicate(stdin, timeout=timeout_seconds + 30)
                else:
                    def send_open_input():
                        try:
                            child.stdin.write(stdin)
                            child.stdin.flush()
                        except BrokenPipeError:
                            pass
                    sender = threading.Thread(target=send_open_input, daemon=True)
                    sender.start()
                    child.wait(timeout=timeout_seconds + 30)
                    sender.join(timeout=5)
                    child.stdin.close()
            except subprocess.TimeoutExpired:
                timed_out = True
                child.kill()
                child.communicate()
            ended = time.monotonic_ns()
        result = {'replica': folder.name, 'sandbox_id': client.sandbox_id(vm), 'host_launch_monotonic_ns': launched,
                  'host_reaped_monotonic_ns': ended, 'ssh_exit_code': child.returncode,
                  'transport_timed_out': timed_out}
        artifacts = {}
        for name in (f'pmu-profile-{execution_id}.json', f'cgroup-resource-{execution_id}.json'):
            raw = client.read_file(vm, '/var/lib/clawtune/artifacts/tool-resource/' + name)
            (folder / name).write_text(raw, encoding='utf-8')
            artifacts[name] = json.loads(raw)
            if artifacts[name].get('execution_id') != execution_id:
                raise RuntimeError(f'replica artifact identity mismatch: {name}')
        bridge_text = client.read_file(vm, '/var/lib/clawtune/artifacts/tool-bridge.jsonl')
        bridges = [json.loads(line) for line in bridge_text.splitlines() if line.strip()]
        exact = [b for b in bridges if b.get('execution_id') == execution_id]
        if len(exact) != 1 or exact[0].get('command_sha256') != command_sha256:
            raise RuntimeError('replica bridge identity or command digest mismatch')
        _write_json(folder / 'bridge.json', exact[0])
        from clawtune_sidecar.monitoring.pmu import quality_gated_pmu_metrics
        profile = artifacts[f'pmu-profile-{execution_id}.json']
        if profile.get('execution_id') != execution_id:
            raise RuntimeError('replica PMU execution identity mismatch')
        metrics = quality_gated_pmu_metrics(profile)
        result.update(pmu_valid=bool(metrics['eligible'] and metrics.get('instructions')),
                      bridge=exact[0], pmu_metrics=metrics)
        _write_json(folder / 'result.json', result)
        return result

    def execute_via_runtime(prepared):
        guest_root = '/tmp/clawbox-placement-' + uuid.uuid4().hex
        created = client.run_command(runtime_vm, 'mkdir -m 700 '+guest_root, timeout_s=20)
        if created.exit_code:
            raise RuntimeError('could not prepare Runtime placement controller')
        client.write_file(runtime_vm, guest_root+'/key', credentials.client_private)
        client.write_file(runtime_vm, guest_root+'/known_hosts', 'clawbox-placement '+credentials.host_public+'\n')
        client.write_file(runtime_vm, guest_root+'/launch.py', RUNTIME_LAUNCHER)
        client.write_file(runtime_vm, guest_root+'/input.json', json.dumps({
            'remote_command': remote_command, 'stdin_b64': base64.b64encode(stdin).decode(), 'real_ssh': real_ssh,
            'stdin_eof': stdin_eof, 'timeout_seconds': timeout_seconds,
            'replicas': [{'index': i, 'host': route.host, 'port': route.port}
                         for i, (_, route, _) in enumerate(prepared)]}))
        launched = client.run_command(runtime_vm,
            'chmod 600 '+guest_root+'/key; /opt/clawtune/venv/bin/python '+guest_root+'/launch.py '+guest_root,
            timeout_s=timeout_seconds+150)
        if launched.exit_code:
            raise RuntimeError('Runtime replica controller failed: '+launched.stderr[-2000:])
        controller = json.loads(client.read_file(runtime_vm, guest_root+'/result.json'))
        record.update(clock_domain='runtime-controller-monotonic',
                      timing_definition='common Runtime SSH launch release to last SSH process reap',
                      release=controller['release'])
        _write_json(output/'controller.json', controller)
        from clawtune_sidecar.monitoring.pmu import quality_gated_pmu_metrics
        for result in controller['replicas']:
            vm, _, folder = prepared[result['index']]
            for channel in ('stdout', 'stderr'):
                (folder/(channel+'.bin')).write_bytes(base64.b64decode(result.pop(channel+'_b64')))
            result.update(replica=folder.name, sandbox_id=client.sandbox_id(vm))
            for name in ('pmu-profile', 'cgroup-resource'):
                filename=f'{name}-{execution_id}.json'
                raw=client.read_file(vm, '/var/lib/clawtune/artifacts/tool-resource/'+filename)
                (folder/filename).write_text(raw, encoding='utf-8')
                parsed=json.loads(raw)
                if parsed.get('execution_id') != execution_id:
                    raise RuntimeError('replica artifact execution identity mismatch')
                if name=='pmu-profile':
                    metrics=quality_gated_pmu_metrics(parsed)
                    result.update(pmu_valid=bool(metrics['eligible'] and metrics.get('instructions')), pmu_metrics=metrics)
            lines=client.read_file(vm, '/var/lib/clawtune/artifacts/tool-bridge.jsonl').splitlines()
            exact=[r for line in lines if line.strip() for r in [json.loads(line)] if r.get('execution_id')==execution_id]
            if len(exact)!=1 or exact[0].get('command_sha256')!=command_sha256:
                raise RuntimeError('replica bridge identity or command digest mismatch')
            result['bridge']=exact[0]
            _write_json(folder/'bridge.json', exact[0])
            _write_json(folder/'result.json', result)
            record['replicas'].append(result)
        rows=record['replicas']
        record.update(makespan_seconds=(max(r['controller_reaped_monotonic_ns'] for r in rows)-controller['release']['monotonic_ns'])/1e9,
                      launch_skew_seconds=(max(r['controller_launch_monotonic_ns'] for r in rows)-min(r['controller_launch_monotonic_ns'] for r in rows))/1e9)
        client.run_command(runtime_vm, 'rm -rf -- '+shlex.quote(guest_root), timeout_s=20)

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            prepared = list(pool.map(create, range(4)))
        record['status'] = 'ready'
        _write_json(output / 'trial.json', record)
        if runtime_vm is not None:
            execute_via_runtime(prepared)
        else:
          with tempfile.TemporaryDirectory(prefix='clawbox-placement-ssh-') as secret_dir:
            key, known_hosts = Path(secret_dir) / 'key', Path(secret_dir) / 'known_hosts'
            key.write_text(credentials.client_private, encoding='utf-8')
            key.chmod(0o600)
            known_hosts.write_text('clawbox-placement ' + credentials.host_public + '\n', encoding='utf-8')
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(execute, item, key, known_hosts) for item in prepared]
                barrier.wait(timeout=60)
                # Inspect every future, retaining completed replicas when another fails.
                errors = []
                for future in futures:
                    try:
                        record['replicas'].append(future.result())
                    except Exception as exc:
                        errors.append(f'{type(exc).__name__}: {exc}')
                if errors:
                    raise RuntimeError('; '.join(errors))
        if runtime_vm is None:
            record.update(release=release,
                makespan_seconds=(max(r['host_reaped_monotonic_ns'] for r in record['replicas']) - release['monotonic_ns']) / 1e9,
                host_launch_skew_seconds=(max(r['host_launch_monotonic_ns'] for r in record['replicas']) - min(r['host_launch_monotonic_ns'] for r in record['replicas'])) / 1e9)
        record['status'] = ('measured' if all(r['pmu_valid'] and not r['transport_timed_out']
                                          and r['ssh_exit_code'] == r['bridge']['exit_code']
                                          and not r['bridge'].get('timed_out')
                                          and not r['bridge'].get('cancelled')
                                          for r in record['replicas']) else 'invalid')
    except Exception as exc:
        record.update(status='invalid', error=f'{type(exc).__name__}: {exc}')
    finally:
        record['created_sandbox_ids'] = [client.sandbox_id(vm) for vm in live]
        if release:
            record['release'] = release
        for vm in live:
            try:
                client.kill_sandbox(vm)
            except Exception as exc:
                record['cleanup_errors'].append(f'{client.sandbox_id(vm)}: {exc}')
        if record['cleanup_errors']:
            record['status'] = 'invalid'
        _write_json(output / 'trial.json', record)
    return record
