"""Stable experiment priority domains, allocated before Cube starts a VM.

The root-owned registry conveys an opaque capability through Cube's persisted
environment. Cubelet resolves it to an already allocated leaf; callers cannot
pass arbitrary host paths. Both VM roles keep the same leaf across restore.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import time
from pathlib import Path
import uuid

REGISTRY = Path('/run/clawbox/cgroups')
TOKEN_ENV = 'CLAWBOX_CGROUP_TOKEN'


def session_priority(index: int, concurrency: int, repetition: int) -> str:
    # Exactly one HIGH session, balanced across paired repetitions. No arm ID,
    # policy, prediction or thread arrival order participates in this mapping.
    return 'high' if index == repetition % concurrency else 'low'


class PriorityDomain:
    def __init__(self, parent: Path, *, concurrency: int, repetition: int,
                 registry: Path = REGISTRY, placements: dict | None = None,
                 journal_directory: Path | None = None, legacy: bool = True):
        self.parent = parent.resolve()
        self.legacy = legacy
        self.registry = registry
        self.placements = placements or {}
        self.observations = []
        self.lock_file = None
        self.prefix = 'clawbox-' + uuid.uuid4().hex
        self.high = self.parent / (self.prefix + '-high')
        self.low = self.parent / (self.prefix + '-low')
        self.paths: list[Path] = []
        self.leaves: dict[tuple[int, str], Path] = {}
        self.priorities = {i: session_priority(i, concurrency, repetition) if legacy else 'neutral'
                           for i in range(concurrency)}
        # Equal sibling defaults would accidentally give one HIGH session half
        # of the CPU shares. Normalize by offered sessions in every arm,
        # including OFF. These are shares, not reservations or measured use.
        self.cpu_weights = {'high': max(1, round(10000/concurrency)),
                            'low': max(1, 10000-round(10000/concurrency))}
        self.tokens = {(i, role): uuid.uuid4().hex for i in self.priorities for role in ('runtime','tool')}
        self.journal = journal_directory / (self.prefix+'.json') if journal_directory else None

    def _journal(self):
        if self.journal is None:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open('x', encoding='utf-8') as stream:
            os.chmod(self.journal, 0o600)
            json.dump({'parent':str(self.parent),'prefix':self.prefix,
                       'priorities':self.priorities,'tokens':list(self.tokens.values()),
                       'flat': not self.legacy},stream)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _enable(path: Path):
        controllers = (path / 'cgroup.controllers').read_text().split()
        required = {'memory', 'cpu', 'cpuset'}
        if not required.issubset(controllers):
            raise RuntimeError(f'missing cgroup controllers at {path}: {required-set(controllers)}')
        (path / 'cgroup.subtree_control').write_text('+memory +cpu +cpuset')

    def _create(self, path: Path, *, branch: bool, high: bool = False):
        path.mkdir()
        self.paths.append(path)
        # Explicit descendant protection also works without memory_recursiveprot.
        # The HIGH ancestor's changing memory.low remains the effective budget.
        if high:
            (path / 'memory.low').write_text('max')
        if branch:
            self._enable(path)

    def start(self):
        if os.geteuid() != 0:
            raise RuntimeError('priority domain registration requires a root experiment worker')
        if not self.parent.is_relative_to(Path('/sys/fs/cgroup/cube_sandbox/sandbox')):
            raise ValueError('priority domain must be inside the Cube VM memory subtree')
        self.registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.registry.mkdir(exist_ok=True, mode=0o700)
        for directory in (self.registry.parent, self.registry):
            st = directory.lstat()
            if directory.is_symlink() or st.st_uid != 0 or st.st_mode & 0o077:
                raise RuntimeError('unsafe Cube cgroup registry ownership or permissions')
        self._enable(self.parent)
        try:
            import fcntl
            name = hashlib.sha256(str(self.parent).encode()).hexdigest() + '.lock'
            self.lock_file = (self.registry / name).open('a')
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            events = dict(line.split() for line in (self.parent/'cgroup.events').read_text().splitlines())
            if events.get('populated') != '0':
                raise RuntimeError('physical VM domain is not empty before the arm')
            self._journal()
            if not self.legacy:
                for index in self.priorities:
                    for role in ('runtime', 'tool'):
                        leaf = self.parent / f'{self.prefix}-vm-{index:04d}-{role}'
                        self._create(leaf, branch=False)
                        self.leaves[index, role] = leaf
                        self.prepare(index, role)
                        with (self.registry / self.tokens[index, role]).open('x', encoding='utf-8') as stream:
                            os.chmod(stream.name, 0o600)
                            json.dump({'path': '/' + leaf.relative_to('/sys/fs/cgroup').as_posix()}, stream)
                self.cpu_weights = {'default': 100}
                return
            self._create(self.high, branch=True)
            self._create(self.low, branch=True)
            (self.high/'cpu.weight').write_text(str(self.cpu_weights['high']))
            (self.low/'cpu.weight').write_text(str(self.cpu_weights['low']))
            for index, priority in self.priorities.items():
                session = (self.high if priority == 'high' else self.low) / f'session-{index:04d}'
                self._create(session, branch=True, high=priority == 'high')
                for role in ('runtime', 'tool'):
                    leaf = session / role
                    self._create(leaf, branch=False, high=priority == 'high')
                    token = self.tokens[index, role]
                    self.leaves[index, role] = leaf
                    self.prepare(index, role)
                    with (self.registry / token).open('x', encoding='utf-8') as stream:
                        os.chmod(stream.name, 0o600)
                        json.dump({'path': '/' + leaf.relative_to('/sys/fs/cgroup').as_posix()}, stream)
        except BaseException:
            self.close()
            raise

    def token(self, index: int, role: str) -> str:
        return self.tokens[index, role]

    def prepare(self, index: int, role: str):
        leaf = self.leaves[index, role]
        # Cube's pause/destroy response can precede the final VMM thread leaving
        # its preallocated leaf. Restore may immediately reuse that same leaf.
        deadline = time.monotonic() + 5
        while (leaf/'cgroup.procs').read_text().strip() and time.monotonic() < deadline:
            time.sleep(.05)
        if (leaf/'cgroup.procs').read_text().strip():
            raise RuntimeError(f'priority VM leaf is already populated: {leaf}')
        cpus, node = self.placements.get(index, (None, None))
        if cpus is not None:
            (leaf/'cpuset.cpus').write_text(cpus)
        if node is not None:
            (leaf/'cpuset.mems').write_text(str(node))

    def verify(self, index: int, role: str, sandbox_id: str) -> dict:
        from .topology import parse_cpu_list
        from .numa_borrow import read_numa_lru_bytes
        expected = self.leaves[index, role]
        # The leaf was empty immediately before Cube received the opaque token,
        # and Cubelet can resolve that token only to this root-owned registry
        # entry.  A populated leaf with vCPU threads therefore proves routing
        # without an O(number of host processes) /proc scan for every VM.
        actual = expected.resolve()
        if actual == self.parent or not actual.is_relative_to(self.parent):
            raise RuntimeError(f'Cube preallocation cgroup escaped its domain: {actual}')
        cpus = (actual/'cpuset.cpus.effective').read_text().strip()
        mems = (actual/'cpuset.mems.effective').read_text().strip()
        configured_cpus, configured_node = self.placements.get(index, (None, None))
        required_cpus = configured_cpus or (self.parent/'cpuset.cpus.effective').read_text().strip()
        required_mems = (str(configured_node) if configured_node is not None else
                         (self.parent/'cpuset.mems.effective').read_text().strip())
        if parse_cpu_list(cpus) != parse_cpu_list(required_cpus) or parse_cpu_list(mems) != parse_cpu_list(required_mems):
            raise RuntimeError(f'VM effective cpuset mismatch at {actual}: CPUs={cpus}, memory={mems}')
        threads = []
        for tid in (actual/'cgroup.threads').read_text().split():
            try:
                status = dict(line.split(':', 1) for line in Path(f'/proc/{tid}/status').read_text().splitlines() if ':' in line)
            except FileNotFoundError:
                continue  # auxiliary I/O threads may exit during observation
            allowed = status['Cpus_allowed_list'].strip()
            if not parse_cpu_list(allowed) <= parse_cpu_list(cpus):
                raise RuntimeError(f'VM thread {tid} escaped compute cpuset')
            threads.append({'tid':int(tid), 'name':status['Name'].strip(), 'cpus_allowed':allowed})
        if not any(t['name'].startswith('vcpu') for t in threads):
            raise RuntimeError(f'Cube preallocation cgroup routing failed: no vCPU threads in {actual}')
        resident, unattributed = read_numa_lru_bytes(actual)
        evidence = {'priority': self.priorities[index], 'cgroup': str(actual),
                'sandbox_id': sandbox_id, 'role': role, 'compute_numa':configured_node,
                'effective_cpus':cpus, 'memory_nodes':sorted(parse_cpu_list(mems)),
                'actual_residency_bytes':resident, 'unattributed_bytes':unattributed,
                'threads':threads, 'enforcement':'preallocation_cgroup'}
        self.observations.append(evidence)
        return evidence

    def close(self):
        # Revoke capabilities first; never recursively delete an arbitrary path.
        for token in self.tokens.values():
            (self.registry / token).unlink(missing_ok=True)
        # Cube's destroy response can precede the final VMM thread leaving its
        # cgroup by a few seconds.  Retry the same known leaves rather than
        # making the next qualification or arm fail on that teardown race.
        pending = list(reversed(self.paths))
        deadline = time.monotonic() + 15
        last_errors: dict[Path, OSError] = {}
        while pending:
            retry = []
            for path in pending:
                try:
                    path.rmdir()
                    last_errors.pop(path, None)
                except FileNotFoundError:
                    last_errors.pop(path, None)
                except OSError as exc:
                    retry.append(path)
                    last_errors[path] = exc
            if not retry or time.monotonic() >= deadline:
                pending = retry
                break
            pending = retry
            time.sleep(0.1)
        # Cube may lose the API record while an owned shim remains alive.  At
        # this point every sandbox has already been destroyed and capabilities
        # are revoked, so terminate processes only inside our exact leaves.
        if pending:
            for path in pending:
                kill = path / 'cgroup.kill'
                try:
                    if kill.exists():
                        kill.write_text('1')
                except FileNotFoundError:
                    pass
            deadline = time.monotonic() + 5
            while pending:
                retry = []
                for path in pending:
                    try:
                        path.rmdir()
                        last_errors.pop(path, None)
                    except FileNotFoundError:
                        last_errors.pop(path, None)
                    except OSError as exc:
                        retry.append(path)
                        last_errors[path] = exc
                if not retry or time.monotonic() >= deadline:
                    pending = retry
                    break
                pending = retry
                time.sleep(0.1)
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None
        if pending:
            raise RuntimeError(
                'priority domain cleanup failed: '
                + '; '.join(str(last_errors[path]) for path in pending)
            )
        if self.journal is not None:
            self.journal.unlink(missing_ok=True)


def cleanup_priority_domains(directory: Path):
    """Supervisor recovery after a worker is forcibly killed; VMs must die first."""
    import fcntl
    REGISTRY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    REGISTRY.mkdir(exist_ok=True, mode=0o700)
    for folder in (REGISTRY.parent, REGISTRY):
        st=folder.lstat()
        if folder.is_symlink() or st.st_uid!=0 or st.st_mode&0o077:
            raise RuntimeError('unsafe priority cleanup registry')
    for journal in directory.glob('clawbox-*.json'):
        st=journal.lstat()
        if journal.is_symlink() or st.st_uid != 0 or st.st_mode & 0o077:
            raise RuntimeError('unsafe priority cleanup journal')
        record=json.loads(journal.read_text())
        prefix=record['prefix']; parent=Path(record['parent']).resolve()
        if (not re.fullmatch(r'clawbox-[0-9a-f]{32}',prefix)
                or journal.name != prefix+'.json'
                or not parent.is_relative_to(Path('/sys/fs/cgroup/cube_sandbox/sandbox'))):
            raise ValueError('invalid priority cleanup domain')
        name=hashlib.sha256(str(parent).encode()).hexdigest()+'.lock'
        with (REGISTRY/name).open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for token in record['tokens']:
                if not re.fullmatch(r'[0-9a-f]{32}',token):
                    raise ValueError('invalid priority cleanup token')
                (REGISTRY/token).unlink(missing_ok=True)
            paths=[]
            for index,priority in record['priorities'].items():
                if not index.isdecimal() or priority not in ('high','low','neutral'):
                    raise ValueError('invalid priority cleanup session')
                if record.get('flat'):
                    paths.extend(parent / f'{prefix}-vm-{int(index):04d}-{role}'
                                 for role in ('runtime', 'tool'))
                    continue
                session=parent/(prefix+'-'+priority)/f'session-{int(index):04d}'
                paths.extend([session/'runtime',session/'tool',session])
            if not record.get('flat'):
                paths.extend([parent/(prefix+'-high'),parent/(prefix+'-low')])
            for path in paths:
                try:
                    path.rmdir()
                except FileNotFoundError:
                    pass
            journal.unlink()
