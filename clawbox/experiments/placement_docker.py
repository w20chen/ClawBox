"""Run the frozen, checkpoint-restored four-tool placement experiment on kunpeng."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

ROOT = Path(os.environ.get('PLACEMENT_FORMAL_ROOT', '/tmp/placement-formal-20260914'))
PLAN = json.loads((ROOT / 'plan.json').read_text())
PLAN_HASH = hashlib.sha256((ROOT / 'plan.json').read_bytes()).hexdigest()
POOL = PLAN['topology_selection']['pool']
CLUSTERS = PLAN['topology_selection']['cores']
PLACEMENTS = PLAN['placement_pairs']


def run(*args, timeout=120, check=True):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=check)


def expand(spec):
    result = []
    for part in spec.split(','):
        edge = part.split('-')
        result.extend(range(int(edge[0]), int(edge[-1]) + 1))
    return sorted(result)


def cgroup_state(name, intended):
    pid = int(run('docker', 'inspect', '-f', '{{.State.Pid}}', name).stdout)
    status = Path(f'/proc/{pid}/status').read_text()
    cgroup = Path('/sys/fs/cgroup' + Path(f'/proc/{pid}/cgroup').read_text().strip().split('::', 1)[1])
    allowed = expand(re.search(r'^Cpus_allowed_list:\s*(\S+)', status, re.M).group(1))
    effective = expand((cgroup / 'cpuset.cpus.effective').read_text().strip())
    mems = expand((cgroup / 'cpuset.mems.effective').read_text().strip())
    maxmem = (cgroup / 'memory.max').read_text().strip()
    verified = allowed == intended == effective and mems == [0] and maxmem == str(6 * 1024**3)
    if not verified:
        raise RuntimeError(f'cgroup mismatch {name}: allowed={allowed} effective={effective} mems={mems} maxmem={maxmem}')
    return {'host_init_pid': pid, 'cgroup_path': str(cgroup), 'allowed_host_cpus': allowed,
            'effective_mems': mems, 'memory_max_bytes': int(maxmem)}


def host_wrapper_pid(name):
    rows = run('docker', 'top', name, '-eo', 'pid,args').stdout.splitlines()[1:]
    found = [int(row.split()[0]) for row in rows if '/input/wrapper.sh' in row]
    if len(found) != 1:
        raise RuntimeError(f'wrapper PID ambiguous in {name}: {rows}')
    return found[0]


def mapping(placement, round_id):
    if placement == 'linux':
        return {letter: POOL for letter in 'ABCD'}
    result = {}
    for group_idx, group in enumerate(PLACEMENTS[placement]):
        cores = list(CLUSTERS[group_idx])
        if (round_id + list(PLACEMENTS).index(placement)) % 2:
            cores.reverse()
        result.update({letter: [cores[idx]] for idx, letter in enumerate(group)})
    return result


def run_trial(round_id, placement, attempt=1, warmup=False):
    trial_name = f'{"warm" if warmup else "r" + str(round_id)}-{placement}-{attempt}'
    trial_dir = ROOT / trial_name
    trial_dir.mkdir(exist_ok=False)
    barrier = trial_dir / 'barrier'
    barrier.mkdir(exist_ok=True)
    mapped = mapping(placement, round_id)
    containers = {}
    runners = {}
    perf = {}
    states = {}
    try:
        for letter in 'ABCD':
            target = PLAN['targets'][letter]
            leaf = trial_dir / letter
            input_dir = leaf / 'input'
            result_dir = leaf / 'result'
            input_dir.mkdir(parents=True, exist_ok=True)
            result_dir.mkdir(exist_ok=True)
            (input_dir / 'target.sh').write_text(target['expected']['command'], encoding='utf-8')
            (input_dir / 'wrapper.sh').write_text('''#!/bin/bash
set +e
touch /result/ready
while [ ! -e /barrier/go ]; do sleep 0.02; done
start=$(date +%s%N)
bash /input/target.sh > /result/stdout 2> /result/stderr
code=$?
end=$(date +%s%N)
printf '%s %s %s\\n' "$start" "$end" "$code" > /result/timing
exit "$code"
''')
            name = f'placement-formal-{trial_name}-{letter.lower()}'
            cpus = ','.join(map(str, mapped[letter]))
            args = ['docker', 'run', '--platform', 'linux/amd64', '-d', '--name', name,
                    '--cpuset-cpus', cpus, '--cpuset-mems', '0', '--memory', '6g', '--memory-swap', '6g',
                    '--pids-limit', '512', '--network', 'none', '-v', f'{input_dir}:/input:ro',
                    '-v', f'{result_dir}:/result', '-v', f'{barrier}:/barrier:ro']
            if letter in 'CD':
                args += ['-e', 'PATH=/opt/conda/envs/testbed/bin:/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                         '-e', 'CONDA_PREFIX=/opt/conda/envs/testbed', '-w', '/workspace']
            else:
                args += ['-w', '/app']
            args += [target['checkpoint_image'], 'sleep', 'infinity']
            run(*args, timeout=90)
            containers[letter] = name
            states[letter] = cgroup_state(name, mapped[letter])
            image_id = run('docker', 'image', 'inspect', '-f', '{{.Id}}', target['checkpoint_image']).stdout.strip()
            live_id = run('docker', 'inspect', '-f', '{{.Image}}', name).stdout.strip()
            if image_id != live_id:
                raise RuntimeError(f'checkpoint image mismatch {letter}')
            states[letter]['checkpoint_image_id'] = image_id
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            future = {letter: pool.submit(subprocess.Popen, ['docker', 'exec', name, 'bash', '/input/wrapper.sh'],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                      for letter, name in containers.items()}
            runners = {letter: f.result() for letter, f in future.items()}
        deadline = time.monotonic() + 45
        while not all((trial_dir / letter / 'result/ready').exists() for letter in 'ABCD'):
            if time.monotonic() > deadline:
                raise TimeoutError('barrier readiness timeout')
            time.sleep(.02)
        for letter, name in containers.items():
            states[letter]['host_wrapper_pid'] = host_wrapper_pid(name)
            cg = Path(states[letter]['cgroup_path'])
            states[letter]['cpu_stat_before'] = (cg / 'cpu.stat').read_text()
            if not warmup:
                perf_file = trial_dir / letter / 'perf.csv'
                perf[letter] = subprocess.Popen(['sudo', '-n', 'perf', 'stat', '-x,', '-o', str(perf_file),
                                                 '-a', '-C', ','.join(map(str, mapped[letter])),
                                                 '-e', 'LLC-loads,LLC-load-misses,cycles,instructions',
                                                 '-G', str(cg.relative_to('/sys/fs/cgroup'))],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(.15)
        (barrier / 'go').touch()
        for letter, proc in runners.items():
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                raise TimeoutError(f'tool {letter} exceeded 120s')
        for letter, proc in perf.items():
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)
        tools = {}
        for letter, name in containers.items():
            target = PLAN['targets'][letter]['expected']
            leaf = trial_dir / letter
            raw = (leaf / 'result/timing').read_text().split()
            start_ns, end_ns, code = map(int, raw)
            stdout = (leaf / 'result/stdout').read_text(errors='replace')
            stderr = (leaf / 'result/stderr').read_text(errors='replace')
            expected = target['expected_stdout']
            output_ok = (stdout == expected or stdout.rstrip('\n') == expected.rstrip('\n'))
            if code != target['expected_exit_code'] or not output_ok or 'Segmentation fault' in stderr:
                raise RuntimeError(f'target mismatch {letter}: code={code}, stdout={stdout!r}, stderr={stderr!r}')
            cg = Path(states[letter]['cgroup_path'])
            states[letter]['cpu_stat_after'] = (cg / 'cpu.stat').read_text()
            states[letter]['memory_numa_stat'] = (cg / 'memory.numa_stat').read_text()
            tools[letter] = {'started_s': start_ns / 1e9, 'ended_s': end_ns / 1e9,
                             'duration_s': (end_ns - start_ns) / 1e9,
                             'exit_code': code, 'stdout_sha256': hashlib.sha256(stdout.encode()).hexdigest(),
                             'checkpoint_verified': True, 'mapping_verified': True,
                             'allowed_host_cpus': states[letter]['allowed_host_cpus'],
                             'effective_mems': states[letter]['effective_mems'],
                             'host_wrapper_pid': states[letter]['host_wrapper_pid'],
                             'checkpoint_image_id': states[letter]['checkpoint_image_id'],
                             'cpu_stat_before': states[letter]['cpu_stat_before'],
                             'cpu_stat_after': states[letter]['cpu_stat_after'],
                             'memory_numa_stat': states[letter]['memory_numa_stat'],
                             'perf_stat': (leaf / 'perf.csv').read_text() if not warmup and (leaf / 'perf.csv').exists() else None}
        row = {'plan_sha256': PLAN_HASH, 'group_id': 'G1', 'round': round_id, 'placement': placement,
               'attempt': attempt, 'status': 'verified', 'tools': tools,
               'makespan_s': max(x['ended_s'] for x in tools.values()) - min(x['started_s'] for x in tools.values())}
        (trial_dir / 'trial.json').write_text(json.dumps(row, indent=2) + '\n')
        return row
    finally:
        for proc in perf.values():
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                try: proc.wait(timeout=3)
                except subprocess.TimeoutExpired: proc.kill()
        for proc in runners.values():
            if proc.poll() is None:
                proc.terminate()
        for name in containers.values():
            run('docker', 'rm', '-f', name, timeout=30, check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--warmup', action='store_true')
    parser.add_argument('--round', type=int)
    parser.add_argument('--placement', choices=['P1', 'P2', 'P3', 'linux'])
    args = parser.parse_args()
    if args.warmup:
        for repeat in (1, 2):
            row = run_trial(0, 'linux', repeat, warmup=True)
            print(f'warmup {repeat}: {row["makespan_s"]:.3f}s', flush=True)
    elif args.round and args.placement:
        for attempt in range(1, 4):
            try:
                row = run_trial(args.round, args.placement, attempt)
                print(json.dumps({'round': args.round, 'placement': args.placement,
                                  'attempt': attempt, 'makespan_s': row['makespan_s'],
                                  'tool_s': {k: v['duration_s'] for k, v in row['tools'].items()}}), flush=True)
                break
            except Exception as error:
                print(f'failed {args.round} {args.placement} attempt {attempt}: {error}', flush=True)
                if attempt == 3: raise
    else:
        parser.error('specify --warmup or --round and --placement')


if __name__ == '__main__':
    main()
