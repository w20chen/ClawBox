"""Bounded, isolated IRQ/cgroup tracing alongside clawbox experiment.

Does not launch VMs. Run as root on the experiment host before launch.
Raw entry/return timestamps include time sleeping in the kernel.
"""
import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import time
import uuid


FUNCTIONS = (
    'kvm_irqfd_assign', 'irq_bypass_register_consumer',
    'irq_bypass_unregister_consumer', 'kvm_set_irq_routing',
    'cpuset_post_attach',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path,
                        help='New evidence directory; existing directories are refused')
    parser.add_argument('--seconds', type=int, default=3600,
                        help='Capture limit in seconds (default 3600)')
    parser.add_argument('--trace-root', type=Path, default=Path('/sys/kernel/tracing'))
    parser.add_argument('--log-root', type=Path, default=Path('/data/log'))
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error('--seconds must be positive')
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        parser.error('Run with sudo on the Linux experiment host')
    trace = args.trace_root
    if not (trace / 'kprobe_events').exists():
        parser.error('tracefs with CONFIG_KPROBE_EVENTS is required; use --trace-root for its mount')
    args.output.mkdir(parents=True, exist_ok=False)
    group = 'clawbox_create_' + uuid.uuid4().hex[:12]
    instance = trace / 'instances' / group
    registered = []
    logs = {}
    seen_tids = set()
    stopped = False
    fd = None
    status = 'error'
    started = time.time()

    def stop(*_):
        nonlocal stopped
        stopped = True

    def save(name, value):
        (args.output / name).write_text(json.dumps(value, indent=2), encoding='utf-8')

    def probe(command):
        # tracefs command files reject O_APPEND; O_TRUNC would clear other probes.
        command_fd = os.open(trace / 'kprobe_events', os.O_WRONLY)
        try:
            os.write(command_fd, (command + '\n').encode('ascii'))
        finally:
            os.close(command_fd)

    def copy_logs(initial=False):
        # Capture only new bytes, including new files/rotations during this run.
        for directory in ('CubeAPI', 'CubeMaster', 'Cubelet', 'CubeShim', 'CubeVmm'):
            for path in (args.log_root / directory).glob('*.log'):
                try:
                    stat = path.stat()
                    key = (str(path), stat.st_ino)
                    if key not in logs:
                        src = path.open('rb')
                        if initial:
                            src.seek(0, 2)
                        dst = (args.output / f'{directory}-{path.name}-{stat.st_ino}').open('xb')
                        logs[key] = (src, dst)
                except FileNotFoundError:
                    continue
        for src, dst in logs.values():
            data = src.read(4 * 1024 * 1024)
            if data:
                dst.write(data)
                dst.flush()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    try:
        instance.mkdir()
        (instance / 'tracing_on').write_text('0')
        (instance / 'trace_clock').write_text('mono')
        (instance / 'buffer_size_kb').write_text('256')
        for fn in FUNCTIONS:
            for kind, prefix in (('enter', 'p'), ('exit', 'r4096')):
                name = f'{fn}_{kind}'
                probe(f'{prefix}:{group}/{name} {fn}')
                registered.append(name)
        copy_logs(initial=True)
        fd = os.open(instance / 'trace_pipe', os.O_RDONLY | os.O_NONBLOCK)
        (instance / 'events' / group / 'enable').write_text('1')
        (instance / 'tracing_on').write_text('1')
        save('ready.json', {'pid': os.getpid(), 'group': group,
             'unix_s': time.time(), 'monotonic_s': time.monotonic(),
             'kernel': os.uname().release, 'functions': FUNCTIONS,
             'seconds': args.seconds, 'scope': 'host-wide; correlate TIDs and VM IDs',
             'log_offsets': {str(k): v[0].tell() for k, v in logs.items()}})
        print('Creation trace ready', flush=True)
        deadline = time.monotonic() + args.seconds
        next_logs = 0
        pending = b''
        with (args.output / 'kernel-trace.txt').open('xb') as raw, \
             (args.output / 'thread-identities.jsonl').open('x', encoding='utf-8') as ids:
            while not stopped and time.monotonic() < deadline:
                if select.select([fd], [], [], 1)[0]:
                    try:
                        data = os.read(fd, 1024 * 1024)
                    except BlockingIOError:
                        continue
                    raw.write(data)
                    raw.flush()
                    lines = (pending + data).split(b'\n')
                    pending = lines.pop()
                    for line in lines:
                        match = re.search(rb'-(\d+)\s+\[\d+\]', line)
                        if not match:
                            continue
                        tid = int(match[1])
                        if tid in seen_tids:
                            continue
                        entry = {'tid': tid, 'unix_s': time.time()}
                        for name in ('status', 'cgroup', 'comm', 'stat', 'cmdline'):
                            try:
                                entry[name] = (Path('/proc') / str(tid) / name).read_text()
                            except OSError:
                                entry[name] = None
                        if entry['status'] is not None:
                            seen_tids.add(tid)
                        ids.write(json.dumps(entry) + '\n')
                    ids.flush()
                if time.monotonic() >= next_logs:
                    copy_logs()
                    next_logs = time.monotonic() + 5
            (instance / 'tracing_on').write_text('0')
            while True:
                try:
                    data = os.read(fd, 1024 * 1024)
                except BlockingIOError:
                    break
                if not data:
                    break
                raw.write(data)
        status = 'stopped' if stopped else 'duration_limit'
    finally:
        cleanup_errors = []
        if instance.exists():
            (instance / 'tracing_on').write_text('0')
            stats = {str(p.relative_to(instance)): p.read_text()
                     for p in (instance / 'per_cpu').glob('cpu*/stats')}
            profile = (trace / 'kprobe_profile').read_text()
            # Some kernels omit group names in kprobe_profile. Preserve all
            # rows instead of silently losing missed-return counters.
            save('coverage.json', {'buffers': stats, 'probe_group': group,
                                  'registered_events': registered,
                                  'kprobe_profile_host_wide': profile.splitlines()})
            enable = instance / 'events' / group / 'enable'
            if enable.exists():
                enable.write_text('0')
        if fd is not None:
            os.close(fd)
        for name in reversed(registered):
            try:
                probe(f'-:{group}/{name}')
            except OSError as exc:
                cleanup_errors.append(str(exc))
        if instance.exists():
            try:
                instance.rmdir()
            except OSError as exc:
                cleanup_errors.append(str(exc))
        copy_logs()
        for src, dst in logs.values():
            src.close()
            dst.close()
        save('finished.json', {'status': status, 'started_unix_s': started,
             'finished_unix_s': time.time(), 'cleanup_errors': cleanup_errors})
        if cleanup_errors:
            raise RuntimeError(f'Trace cleanup failed for {group}: {cleanup_errors}')


if __name__ == '__main__':
    main()
