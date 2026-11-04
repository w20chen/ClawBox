"""Replay-boundary orchestration for the supported experiment worker."""
from __future__ import annotations

import base64
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import random
import shlex
import uuid

from .tool_placement import ReplicaPlacement, inspect_cpu, run_four_replica_trial, validate_group_isolation

PYTHON = '/opt/clawtune/venv/bin/python'
PROBE_ROOT = '/run/clawbox-placement'
PROBE = '''import json, os, socket
root='/run/clawbox-placement'
os.makedirs(root, exist_ok=True)
path=root+'/memory.sock'
if os.path.exists(path): os.unlink(path)
server=socket.socket(socket.AF_UNIX); server.bind(path); server.listen(8)
state=None
while True:
    connection,_=server.accept()
    with connection:
        message=json.loads(connection.recv(8192))
        if message['operation']=='set':
            state=message['state']
            with open(root+'/filesystem.json','w') as stream:
                json.dump(state,stream); stream.flush(); os.fsync(stream.fileno())
        connection.sendall(json.dumps({'memory':state,'filesystem':json.load(open(root+'/filesystem.json'))}).encode())
'''


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding='utf-8')


def probe(client, vm, state=None):
    request = {'operation': 'query'} if state is None else {'operation': 'set', 'state': state}
    code = ("import socket,json; s=socket.socket(socket.AF_UNIX); s.settimeout(10); "
            f"s.connect({PROBE_ROOT+'/memory.sock'!r}); "
            f"s.sendall({json.dumps(request).encode()!r}); "
            "print(s.recv(16384).decode()); s.close()")
    result = client.run_command(vm, PYTHON + ' -c ' + shlex.quote(code), timeout_s=20)
    if result.exit_code:
        raise RuntimeError('snapshot state witness unavailable: ' + result.stderr[-1000:])
    return json.loads(result.stdout.strip())


class StudyCatalog:
    def __init__(self, arm, output):
        self.config = arm.execution.tool_placement
        self.output = output
        self.targets = defaultdict(dict)
        self.prefixes = {}
        targets_raw = Path(self.config.targets_file).read_bytes()
        sequences_raw = Path(self.config.prefix_sequences_file).read_bytes()
        output.mkdir(parents=True, exist_ok=True)
        (output/'targets-input.jsonl').write_bytes(targets_raw)
        (output/'prefix-input.jsonl').write_bytes(sequences_raw)
        targets = {r['target_id']: r for line in targets_raw.splitlines() if line.strip()
                   for r in [json.loads(line)]}
        cases = {case.case_id for case in (arm.case, *arm.session_cases)}
        for line in sequences_raw.splitlines():
            sequence = json.loads(line)
            case, rows = sequence['case_id'], sequence['executions']
            if case not in cases:
                continue
            self.prefixes[case] = rows
            occurrences = Counter()
            selected = 0
            for row in rows:
                key = row['operation'], row['profile_command_sha256']
                occurrences[key] += 1
                target = targets[row['target_id']]
                if target['stratum'] not in self.config.strata:
                    continue
                if self.config.max_targets_per_case is not None and selected >= self.config.max_targets_per_case:
                    continue
                self.targets[case][(*key, occurrences[key])] = target
                selected += 1
        nodes = {node.node_id: node for node in arm.resources.compute_nodes}
        self.groups, self.parent_groups = {}, {}
        from .topology import parse_cpu_list
        index = arm.concurrency
        for slot_index, (group, slot) in enumerate(zip(self.config.groups, arm.execution.session_cpu_slots)):
            group_id = f'{group.parent_node_id}-slot{slot_index}'
            self.parent_groups[tuple(sorted(parse_cpu_list(slot.cpus)))] = group_id
            layouts = {}
            for name in ('packed', 'spread', 'within_numa'):
                cpus = getattr(group, name)
                if cpus is None:
                    continue
                replicas = []
                for cpu in cpus:
                    replicas.append(ReplicaPlacement(cpu, inspect_cpu(cpu)['numa_node'], index))
                    index += 1
                layouts[name] = tuple(replicas)
            self.groups[group_id] = layouts
        self.topology = validate_group_isolation(self.groups, isolation_domain=self.config.isolation_domain)
        # Parent prefix work must remain in its own reserved outer LLC set.
        from .topology import parse_cpu_list
        for slot in arm.execution.session_cpu_slots:
            key = self.parent_groups[tuple(sorted(parse_cpu_list(slot.cpus)))]
            llcs = {tuple(r[self.config.isolation_domain]) for rows in self.topology[key].values() for r in rows}
            if any(tuple(inspect_cpu(cpu)[self.config.isolation_domain]) not in llcs for cpu in parse_cpu_list(slot.cpus)):
                raise ValueError('parent session lies outside its reserved trial LLC domains')
        write(output/'catalog.json', {
            'targets_sha256': hashlib.sha256(targets_raw).hexdigest(),
            'prefixes_sha256': hashlib.sha256(sequences_raw).hexdigest(),
            'selected_targets': sum(map(len, self.targets.values())),
            'matching': 'operation and profile-command hash occurrence within recorded trace; observed ordering retained',
            'topology': self.topology,
            'isolation_domain': self.config.isolation_domain,
        })

    def register(self, domain):
        for layouts in self.groups.values():
            for replicas in layouts.values():
                for p in replicas:
                    domain.register_replica_slot(p.domain_index, p.cpu, p.numa_node)


class BoundaryStudy:
    def __init__(self, *, catalog, arm, client, domain, ownership, credentials,
                 runtime_vm, tool_vm, network_allow_out, network_deny_out, emit, parent_cpus, create_gate):
        self.catalog, self.arm, self.client, self.domain = catalog, arm, client, domain
        self.ownership, self.credentials = ownership, credentials
        self.create_gate = create_gate
        self.runtime_vm, self.tool_vm = runtime_vm, tool_vm
        self.allow_out, self.deny_out, self.emit = network_allow_out, network_deny_out, emit
        self.case = arm.case.case_id
        from .topology import parse_cpu_list
        self.layout = catalog.groups[catalog.parent_groups[tuple(sorted(parse_cpu_list(parent_cpus)))]]
        self.root = catalog.output / self.case
        self.root.mkdir(parents=True, exist_ok=False)
        self.counts, self.ordinal = Counter(), 0
        self.pending, self.finished = {}, set()
        self.probe_ready = False
        self.latest_snapshot = None
        if self.case not in catalog.prefixes:
            raise ValueError(f'placement manifest has no prefix for {self.case}')

    def before(self, request):
        if request.get('execution_scope') != 'agent-tool':
            return
        self.ordinal += 1
        key = request['operation'], request['command_sha256']
        self.counts[key] += 1
        target = self.catalog.targets[self.case].get((*key, self.counts[key]))
        with (self.root/'observed-prefix.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'ordinal': self.ordinal, 'request': request,
                                     'target_id': target['target_id'] if target else None})+'\n')
        if target is None:
            return
        capture_path = request.get('placement_capture_path')
        expected_capture = f'/state/openclaw/{self.ownership.session_id.removesuffix("-tool")}/placement-inputs/{request["execution_id"]}.json'
        if capture_path != expected_capture:
            raise ValueError('target has no exact Runtime SSH invocation capture')
        vm = self.tool_vm()
        if not self.probe_ready:
            self.client.write_file(vm, '/tmp/clawbox-placement-probe.py', PROBE)
            result = self.client.run_command(vm,
                f'mkdir -p {PROBE_ROOT}; nohup {PYTHON} /tmp/clawbox-placement-probe.py '
                f'> {PROBE_ROOT}/probe.log 2>&1 < /dev/null &', timeout_s=20)
            if result.exit_code:
                raise RuntimeError('could not start snapshot memory witness')
            ready = self.client.run_command(vm, PYTHON + ' -c ' + shlex.quote(
                "import os,time; p='/run/clawbox-placement/memory.sock'; "
                "deadline=time.monotonic()+10\nwhile not os.path.exists(p) and time.monotonic()<deadline: time.sleep(.02)\n"
                "assert os.path.exists(p), 'memory witness socket not ready'"), timeout_s=15)
            if ready.exit_code:
                raise RuntimeError('snapshot memory witness failed to become ready')
            self.probe_ready = True
        state = {'nonce': uuid.uuid4().hex, 'target_id': target['target_id'],
                 'execution_id': request['execution_id'], 'ordinal': self.ordinal}
        witness = probe(self.client, vm, state)
        if witness != {'memory': state, 'filesystem': state}:
            raise RuntimeError('pre-tool memory/filesystem witness mismatch')
        directory = self.root / target['target_id']
        directory.mkdir()
        write(directory/'baseline-target.json', target)
        write(directory/'request.json', request)
        write(directory/'pre-tool-witness.json', witness)
        snapshot = self.client.create_snapshot(vm, self.ownership)
        # Native incremental snapshots use the previous committed memory base.
        # Keep that base until the next independent snapshot has been committed.
        previous_snapshot = self.latest_snapshot
        self.latest_snapshot = snapshot
        if previous_snapshot is not None:
            self.client.delete_snapshot(previous_snapshot)
        write(directory/'snapshot.json', {'snapshot_id': snapshot,
            'source_sandbox_id': self.client.sandbox_id(vm), 'witness': witness,
            'source_template': self.arm.sandbox.template, 'observed_prefix_ordinal': self.ordinal,
            'retention': 'native snapshot held through trials and the next snapshot commit; manifests and witnesses retained afterward'})
        self.pending[request['execution_id']] = (target, snapshot, witness, directory)
        self.emit({'event': 'tool_placement_snapshot_ready', 'case_id': self.case,
                   'target_id': target['target_id'], 'snapshot_id': snapshot})

    def after(self, request):
        selected = self.pending.pop(request['execution_id'], None)
        if selected is None:
            return
        target, snapshot, witness, directory = selected
        config = self.catalog.config
        results = []
        try:
            capture = json.loads(self.client.read_file(self.runtime_vm(), request['placement_capture_path']))
            if (not capture.get('complete') or capture.get('execution_id') != request['execution_id']
                    or capture.get('profile_command_sha256') != request['command_sha256']):
                raise ValueError('exact SSH input capture is incomplete or belongs to another target')
            stdin = base64.b64decode(capture['stdin_b64'], validate=True)
            if hashlib.sha256(stdin).hexdigest() != capture['stdin_sha256']:
                raise ValueError('captured stdin digest mismatch')
            write(directory/'invocation.json', capture)
            for name in ('pmu-profile', 'cgroup-resource'):
                filename = f'{name}-{request["execution_id"]}.json'
                raw = self.client.read_file(self.tool_vm(), '/var/lib/clawtune/artifacts/tool-resource/'+filename)
                (directory/('parent-'+filename)).write_text(raw, encoding='utf-8')
            def verify(vm):
                actual = probe(self.client, vm)
                if actual != witness:
                    raise RuntimeError('restored native VM did not preserve pre-tool RAM and filesystem state')
                return actual
            rng = random.Random(config.random_seed + int(target['target_id'], 16))
            for repeat in range(config.repetitions):
                order = list(self.layout)
                rng.shuffle(order)
                for name in order:
                    trial = directory / f'repeat-{repeat:02d}-{name}'
                    from .openclaw_driver import openclaw_real_ssh_path
                    result = run_four_replica_trial(client=self.client, domain=self.domain,
                        snapshot_id=snapshot, placements=self.layout[name],
                        ownership=replace(self.ownership, session_id=f'{target["target_id"]}-{repeat}-{name}'),
                        credentials=self.credentials, node_name=self.arm.resources.target_node,
                        remote_command=capture['remote_command'], stdin=stdin, stdin_eof=capture['stdin_eof'],
                        execution_id=request['execution_id'], command_sha256=request['command_sha256'],
                        timeout_seconds=request['tool_timeout_seconds'], output=trial,
                        allow_internet_access=self.arm.sandbox.allow_internet_access,
                        network_allow_out=self.allow_out, network_deny_out=self.deny_out,
                        verify_state=verify, runtime_vm=self.runtime_vm(),
                        real_ssh=openclaw_real_ssh_path(self.ownership.session_id.removesuffix('-tool')),
                        create_gate=self.create_gate)
                    results.append({'repeat': repeat, 'layout': name, 'status': result['status'],
                                    'makespan_seconds': result.get('makespan_seconds'), 'artifact': str(trial/'trial.json')})
                    self.emit({'event': 'tool_placement_trial_complete', 'target_id': target['target_id'], **results[-1]})
                    if result.get('cleanup_errors'):
                        raise RuntimeError('replica cleanup incomplete; refusing to overlap another trial')
            self.finished.add(target['target_id'])
        finally:
            write(directory/'paired-trials.json', results)
            write(directory/'snapshot-retained.json', {'snapshot_id': snapshot,
                  'reason': 'base for the next native incremental snapshot', 'raw_evidence_preserved': True})

    def finish(self):
        expected = {t['target_id'] for t in self.catalog.targets[self.case].values()}
        missing = sorted(expected-self.finished)
        write(self.root/'coverage.json', {'expected': len(expected), 'attempted': len(self.finished),
                                         'missing_target_ids': missing})
        if self.latest_snapshot is not None:
            self.client.delete_snapshot(self.latest_snapshot)
            self.latest_snapshot = None
        if missing:
            raise RuntimeError(f'{len(missing)} selected tools did not reach their replay boundary; see placement coverage')


def qualify_placement(spec, *, artifact_root):
    """One native fork/state/PMU gate for the configured layouts, without replaying a corpus."""
    if spec.execution.tool_placement is None:
        return {'required': False, 'passed': True}
    from clawbox.cube import CubeSandboxClient, OwnedSandboxJournal, Ownership
    from .spec import expand_matrix
    from .priority import PriorityDomain
    from .ssh_credentials import generate_ssh_credentials
    from .openclaw_driver import native_tool_bridge_setup_command
    arm = expand_matrix(spec)[0]
    artifact_root.mkdir(parents=True, exist_ok=False)
    catalog = StudyCatalog(arm, artifact_root/'catalog')
    node = next(n for n in arm.resources.compute_nodes if n.node_id == arm.execution.session_cpu_slots[0].node_id)
    domain = PriorityDomain(Path(arm.resources.local_memory_cgroup), concurrency=1, repetition=0,
                            legacy=False, placements={0: (arm.execution.session_cpu_slots[0].cpus, node.numa_node)},
                            journal_directory=artifact_root/'priority-domains')
    identity = 'placement-qualification-' + uuid.uuid4().hex[:12]
    ownership = Ownership(identity, identity, identity, spec.experiment_id, identity, 'placement-qualification')
    credentials = generate_ssh_credentials()
    client = CubeSandboxClient(journal=OwnedSandboxJournal(artifact_root/'owned-sandboxes.jsonl'),
                               request_timeout_s=arm.execution.cube_request_timeout_seconds)
    vm, snapshot = None, None
    trials = []
    result = {'required': True, 'passed': False, 'trials': trials}
    try:
        domain.start()
        catalog.register(domain)
        domain.prepare(0, 'tool')
        vm = client.create_sandbox(template=arm.sandbox.template, node_name=arm.resources.target_node,
            ownership=ownership, env_vars={'CLAWBOX_CGROUP_TOKEN': domain.token(0, 'tool'),
                'TOOL_MAX_CONCURRENCY': '1', 'CLAWTUNE_PMU_ENABLED': 'true', 'CLAWTUNE_PMU_MAX_ACTIVE': '1',
                'TOOL_BRIDGE_LOG_PATH': '/var/lib/clawtune/artifacts/tool-bridge.jsonl',
                'TOOL_BRIDGE_WORKDIR': arm.sandbox.workspace,
                'CLAWBOX_TOOL_HOST_KEY_B64': base64.b64encode(credentials.host_private.encode()).decode(),
                'CLAWBOX_TOOL_AUTHORIZED_KEY_B64': base64.b64encode((credentials.client_public+'\n').encode()).decode(),
                'TASK_ID': identity, 'CELL_ID': identity}, allow_internet_access=arm.sandbox.allow_internet_access)
        domain.verify(0, 'tool', client.sandbox_id(vm))
        setup = client.run_command(vm, native_tool_bridge_setup_command(
            knowledge_environment={}, require_progress_events=False,
            command_timeout_seconds=arm.execution.command_timeout_seconds,
            allow_internet_access=arm.sandbox.allow_internet_access), timeout_s=60)
        if setup.exit_code:
            raise RuntimeError('placement qualification bridge setup failed: '+setup.stderr[-1000:])
        client.write_file(vm, '/tmp/clawbox-placement-probe.py', PROBE)
        client.run_command(vm, f'mkdir -p {PROBE_ROOT}; nohup {PYTHON} /tmp/clawbox-placement-probe.py '
                           f'> {PROBE_ROOT}/probe.log 2>&1 < /dev/null &', timeout_s=20)
        ready = client.run_command(vm, PYTHON+' -c '+shlex.quote(
            "import os,time; deadline=time.monotonic()+10\n"
            "while not os.path.exists('/run/clawbox-placement/memory.sock') and time.monotonic()<deadline: time.sleep(.02)\n"
            "assert os.path.exists('/run/clawbox-placement/memory.sock')"), timeout_s=15)
        if ready.exit_code:
            raise RuntimeError('qualification state witness did not start')
        witness = probe(client, vm, {'nonce': uuid.uuid4().hex, 'generation': 'before-snapshot'})
        snapshot = client.create_snapshot(vm, ownership)
        changed = probe(client, vm, {'nonce': uuid.uuid4().hex, 'generation': 'after-snapshot'})
        if changed == witness:
            raise RuntimeError('source state did not change after snapshot')
        from .openclaw_driver import native_ssh_route
        import ipaddress
        route = native_ssh_route(client.get_tcp_endpoint(vm, 2222), epoch=1)
        address = ipaddress.ip_address(route.host)
        domain.prepare(0, 'runtime')
        runtime = client.create_sandbox(template=arm.runtime.template, node_name=arm.resources.target_node,
            ownership=replace(ownership, session_id=identity+'-runtime'),
            env_vars={'CLAWBOX_CGROUP_TOKEN': domain.token(0, 'runtime')},
            allow_internet_access=arm.runtime.allow_internet_access,
            network_allow_out=[str(address)+('/32' if address.version==4 else '/128')])
        domain.verify(0, 'runtime', client.sandbox_id(runtime))
        def verify(child):
            actual = probe(client, child)
            if actual != witness:
                raise RuntimeError('native clone lost pre-tool RAM or filesystem state')
            return actual
        command = "printf 'placement-native-probe\\n'"
        execution = 'placement-probe-' + uuid.uuid4().hex
        header = {'v': 1, 'execution_id': execution, 'tool_name': 'exec',
                  'profile_command_b64': base64.urlsafe_b64encode(command.encode()).decode().rstrip('=')}
        remote = '__CBX_EXEC_1__b64:'+base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip('=')+'\n'+command
        for group, layouts in catalog.groups.items():
            for layout_name, placements in layouts.items():
                trial = run_four_replica_trial(client=client, domain=domain, snapshot_id=snapshot,
                    placements=placements, ownership=replace(ownership, session_id=f'{identity}-{group}-{layout_name}'),
                    credentials=credentials, node_name=arm.resources.target_node, remote_command=remote,
                    stdin=b'', execution_id=execution, command_sha256=hashlib.sha256(command.encode()).hexdigest(),
                    timeout_seconds=30, output=artifact_root/group/layout_name,
                    allow_internet_access=arm.sandbox.allow_internet_access, network_allow_out=[], network_deny_out=[],
                    verify_state=verify, runtime_vm=runtime)
                trials.append({'group': group, 'layout': layout_name, 'status': trial['status']})
                if trial['status'] != 'measured':
                    raise RuntimeError(f'placement native gate failed: {group}/{layout_name}; inspect trial.json')
        result.update(passed=True, memory_and_filesystem_restored=True, all_replica_pmu_valid=True)
    finally:
        try:
            client.kill_owned_sandboxes(identity)
        finally:
            domain.close()
            write(artifact_root/'result.json', result)
    return result
