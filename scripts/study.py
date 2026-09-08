#!/usr/bin/env python3
"""Prepare, check, start, and inspect the standalone 13-baseline study."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clawbox.experiments.spec import ExperimentSpec


def read_spec(path):
    value = yaml.safe_load(path.read_text())
    ExperimentSpec.model_validate(value)
    return value


def check(path):
    value = read_spec(path)
    resources = value['resources']
    group = Path(resources['local_memory_cgroup'])
    expected = resources['local_memory_capacity_mib'] * 1024**2
    if (group / 'memory.max').read_text().strip() != str(expected):
        raise ValueError('LOCAL memory is not configured. Run study setup-memory while the VM pool is idle.')
    if not os.access(group / 'memory.reclaim', os.W_OK):
        raise ValueError('LOCAL cache reclaim is not writable. Run study setup-memory.')
    mount = subprocess.check_output(['findmnt', '-n', '-o', 'FSTYPE,OPTIONS',
                                     '--target', resources['warm_snapshot_root']], text=True)
    if not mount.startswith('tmpfs ') or f"mpol=bind:{resources['warm_numa_node']}" not in mount or 'noswap' not in mount:
        raise ValueError('WARM must be a no-swap tmpfs on the configured NUMA node.')
    if not Path(value['workload']['input']).is_file():
        raise ValueError('The replay trace is missing.')
    for name in ('p90_predictions', 'oracle_measurements'):
        if not Path(resources[name]).is_file():
            raise ValueError(f'Missing prediction file: {resources[name]}')
    print('Configuration, trace, prediction files, and memory settings are ready.', flush=True)
    print('This check does not replace the VM, replay, and telemetry validation tests.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create a machine-specific experiment file')
    init.add_argument('--base', type=Path, default=ROOT / 'examples/experiments/tiered-oracle-rec-a-c40.yaml')
    init.add_argument('--trace', type=Path, required=True)
    init.add_argument('--output', type=Path, required=True)
    for name in ('check', 'setup-memory'):
        command = commands.add_parser(name, help='Check prerequisites' if name == 'check' else 'Configure memory on an idle host; uses sudo')
        command.add_argument('--spec', type=Path, required=True)
    start = commands.add_parser('start', help='Start all baselines in the background')
    start.add_argument('--spec', type=Path, required=True)
    start.add_argument('--name', default='study-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))
    start.add_argument('--output-root', type=Path, default=Path(os.environ.get('CLAWBOX_OUTPUT_ROOT', ROOT / 'results')))
    mode = start.add_mutually_exclusive_group()
    mode.add_argument('--steps', type=int, default=5)
    mode.add_argument('--full-trace', action='store_true')
    start.add_argument('--arm-seconds', type=int, help='Per-baseline safety deadline; default 1200 for a prefix, 3600 for the full trace')
    for name in ('status', 'report'):
        command = commands.add_parser(name)
        command.add_argument('directory', type=Path)
    args = parser.parse_args()
    for key in ('base', 'trace', 'output', 'spec', 'output_root', 'directory'):
        if hasattr(args, key):
            setattr(args, key, getattr(args, key).resolve())
    os.chdir(ROOT)
    if args.command == 'init':
        value = read_spec(args.base.resolve())
        if not args.trace.is_file():
            parser.error('Trace does not exist; copy the recording to this machine first.')
        required = ('CUBE_NODE', 'CLAWBOX_RUNTIME_TEMPLATE', 'CLAWBOX_TOOL_TEMPLATE',
                    'CLAWBOX_RUNTIME_IMAGE', 'CLAWBOX_TOOL_IMAGE', 'CLAWBOX_WARM_ROOT', 'CLAWBOX_COLD_ROOT')
        missing = [key for key in required if not os.environ.get(key)]
        if missing:
            parser.error('Set these values in machine.env: ' + ', '.join(missing))
        for role, prefix in (('runtime', 'RUNTIME'), ('sandbox', 'TOOL')):
            ref = os.environ[f'CLAWBOX_{prefix}_IMAGE']
            if '@sha256:' not in ref:
                parser.error(f'CLAWBOX_{prefix}_IMAGE must be the image reference returned after pushing to the registry.')
            value[role].update(template_id=os.environ[f'CLAWBOX_{prefix}_TEMPLATE'],
                               source_image_reference=ref, image_digest=ref.split('@', 1)[1])
        trace = str(args.trace.resolve())
        value['workload']['input'] = trace
        for case in value['workload']['cases']:
            case['replay_trace_reference'] = trace
            case['source_reference'] = trace
        resources = value['resources']
        resources.update(target_node=os.environ['CUBE_NODE'],
                         warm_snapshot_root=os.environ['CLAWBOX_WARM_ROOT'],
                         cold_snapshot_root=os.environ['CLAWBOX_COLD_ROOT'])
        for key in ('p90_predictions', 'oracle_measurements'):
            resources[key] = str((ROOT / resources[key]).resolve())
        ExperimentSpec.model_validate(value)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x') as stream:
            yaml.safe_dump(value, stream, sort_keys=False)
        print(f'Created {args.output}. Review the repository, task prompt, validation command, and VM sizes before starting.')
    elif args.command == 'setup-memory':
        value = read_spec(args.spec.resolve())['resources']
        events = Path(value['local_memory_cgroup']) / 'cgroup.events'
        if events.exists() and 'populated 1' in events.read_text():
            parser.error('The VM pool is in use. Memory setup requires an idle pool.')
        command = ['env', f"CLAWBOX_LOCAL_MIB={value['local_memory_capacity_mib']}",
                   f"CLAWBOX_WARM_MIB={value['warm_memory_capacity_mib']}",
                   f"CLAWBOX_LOCAL_NODE={value['local_numa_node']}",
                   f"CLAWBOX_WARM_NODE={value['warm_numa_node']}", 'bash',
                   str(ROOT / 'scripts/setup-tiered-memory.sh'), value['warm_snapshot_root'],
                   value['cold_snapshot_root'], os.environ.get('SUDO_USER') or os.environ['USER']]
        subprocess.run((['sudo'] if os.geteuid() else []) + command, check=True)
    elif args.command == 'check':
        check(args.spec.resolve())
    elif args.command == 'start':
        if args.steps < 1 or (args.arm_seconds is not None and args.arm_seconds < 1):
            parser.error('Steps and the per-baseline deadline must be positive.')
        if Path(args.name).name != args.name or args.name in ('.', '..'):
            parser.error('Use a run name without directory separators.')
        spec_path = args.spec.resolve()
        check(spec_path)
        group = Path(read_spec(spec_path)['resources']['local_memory_cgroup'])
        if 'populated 1' in (group / 'cgroup.events').read_text():
            parser.error('The VM pool is in use. Wait for the current experiment and cleanup to finish.')
        output = args.output_root.resolve() / args.name
        if output.exists():
            parser.error('That result directory already exists; choose a new --name.')
        args.output_root.mkdir(parents=True, exist_ok=True)
        log = output.with_name(output.name + '.log')
        command = [sys.executable, str(ROOT / 'scripts/run-short-tiered-study.py'),
                   '--spec', str(spec_path), '--output', str(output), '--arm-seconds',
                   str(args.arm_seconds or (3600 if args.full_trace else 1200))]
        command += ['--full-trace'] if args.full_trace else ['--steps', str(args.steps)]
        with log.open('x') as stream:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        print(f'Started in the background (PID {process.pid}).\nResults: {output}\nLog: {log}')
        print(f'Check progress: bash scripts/clawbox study status {output}')
    else:
        if not args.directory.is_dir():
            parser.error('Result directory does not exist. Use the path printed when the study started.')
        helper = 'study-status.py' if args.command == 'status' else 'report-standalone-study.py'
        subprocess.run([sys.executable, str(ROOT / 'scripts' / helper), str(args.directory.resolve())], check=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Cannot complete this command: {exc}')
