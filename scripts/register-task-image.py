"""Register a prepared Tool OCI image as a Cube template in an isolated profile."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONPATH'] = os.pathsep.join(filter(None, [str(ROOT), os.environ.get('PYTHONPATH')]))
from clawbox.lab import apply_environment, template_record


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image', required=True, help='Registry image pinned as repository@sha256:...')
    p.add_argument('--alias', required=True)
    p.add_argument('--profile', type=Path, default=Path.home()/'.config/clawbox/lab.json')
    p.add_argument('--output', type=Path, required=True, help='New task profile; original profile is not changed')
    args = p.parse_args()
    if args.output.exists():
        p.error('Output profile already exists; choose a new path')
    profile = json.loads(args.profile.read_text(encoding='utf-8'))
    apply_environment(profile)
    from cubesandbox import Template
    prior = Template.get(profile['sandbox']['template_id'])
    kernels = {r.get('kernel_version') for r in prior.replicas or [] if r.get('node_id') == profile['node']}
    if len(kernels) != 1 or not next(iter(kernels)):
        raise ValueError('Expected one kernel identity on the selected node')
    cmd = [sys.executable, str(Path(__file__).with_name('register-cube-template.py')), args.image,
           '--alias', args.alias, '--node', profile['node'],
           '--cpu-millicores', str(profile['sandbox']['vcpu']*1000),
           '--memory-mib', str(profile['sandbox']['memory_mib']),
           '--command', '/usr/local/bin/cube-tool-entrypoint.sh',
           '--expected-kernel-version', next(iter(kernels)),
           '--exposed-port', '49983', '--exposed-port', '2222', '--writable-layer-size', '40G']
    result = subprocess.check_output(cmd, text=True)
    identity = json.loads(result.strip().splitlines()[-1])
    record = template_record(identity['template_id'])
    record.pop('nodes')
    profile['sandbox'] = record
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(profile, indent=2)+'\n')
    print(json.dumps({'profile': str(args.output), 'sandbox': record}, indent=2))

if __name__ == '__main__':
    main()
