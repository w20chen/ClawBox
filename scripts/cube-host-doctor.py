#!/usr/bin/env python3
"""Bounded, read-only standalone host inventory. Does not certify experiment gates."""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import subprocess


def probe(*command):
    try:
        r = subprocess.run(command, capture_output=True, text=True, timeout=20)
        return dict(command=command, exit_code=r.returncode, stdout=r.stdout, stderr=r.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(command=command, exit_code=None, error=str(exc))


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New evidence file; refuses overwrite')
    parser.add_argument('--storage', default='/data')
    parser.add_argument('--warm', default='/data/clawbox-tiered/warm')
    args = parser.parse_args()
    if platform.system() != 'Linux':
        parser.error('run this on the Linux KVM host')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        report = dict(schema_version=1, collected_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      hostname=platform.node(), kernel=platform.release(),
                      boot_id=read('/proc/sys/kernel/random/boot_id'), uptime=read('/proc/uptime'),
                      kvm_access=os.access('/dev/kvm', os.R_OK | os.W_OK),
                      controllers=read('/sys/fs/cgroup/cgroup.controllers'),
                      meminfo=read('/proc/meminfo'), numa={}, probes={})
        for node in sorted(Path('/sys/devices/system/node').glob('node[0-9]*')):
            report['numa'][node.name] = dict(cpulist=read(node / 'cpulist'), meminfo=read(node / 'meminfo'))
        commands = {
            'storage': ('findmnt', '-T', args.storage, '-J'),
            'space': ('df', '-B1', args.storage),
            'warm': ('findmnt', '-M', args.warm, '-J'),
            'swap': ('swapon', '--show', '--bytes'),
            'listeners': ('ss', '-lnt'),
            'services': ('systemctl', 'list-units', '--all', '--no-pager', 'cube-sandbox-*'),
            'kernel_faults': ('journalctl', '-k', '-b', '--no-pager',
                              '--grep=soft lockup|hard LOCKUP|BUG:|blocked for more than'),
            'processes': ('ps', '-eo', 'pid,ppid,stat,comm'),
            'docker': ('docker', 'ps', '--format', '{{.Names}} {{.Image}} {{.Status}}'),
        }
        for name, command in commands.items():
            report['probes'][name] = probe(*command)
        # journalctl returns 1 when --grep matches no records, even on a
        # readable, healthy journal. Keep the raw status in the evidence.
        journal = report['probes']['kernel_faults']
        no_faults = (journal['exit_code'] == 1 and
                     journal.get('stdout', '').strip() == '-- No entries --' and
                     not journal.get('stderr', '').strip())
        blockers = []
        if not report['kvm_access']:
            blockers.append('Current user cannot open /dev/kvm')
        if not report['controllers']:
            blockers.append('cgroup v2 controllers unavailable')
        for name in ('storage', 'space', 'services', 'kernel_faults'):
            if report['probes'][name]['exit_code'] != 0 and not (name == 'kernel_faults' and no_faults):
                blockers.append(f'{name} inspection failed; inspect recorded stderr')
        faults = report['probes']['kernel_faults'].get('stdout', '')
        if any(term in faults for term in ('soft lockup', 'hard LOCKUP', 'BUG:', 'blocked for more than')):
            blockers.append('Current boot contains kernel faults; assess host recovery first')
        for unit in ('cube-sandbox-cubelet.service', 'cube-sandbox-cube-api.service'):
            state = probe('systemctl', 'is-active', unit)
            report['probes'][unit] = state
            if state['exit_code'] != 0:
                blockers.append(f'Standalone {unit} is not active')
        report['blockers'] = blockers
        report['formal_ready'] = False
        report['readiness_note'] = 'Inventory only; replay, identity, tier placement and capacity gates remain separate.'
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(f'Evidence: {args.output}')
    for blocker in blockers:
        print(f'BLOCKED: {blocker}')
    print(report['readiness_note'])
    return 2 if blockers else 0


if __name__ == '__main__':
    raise SystemExit(main())
