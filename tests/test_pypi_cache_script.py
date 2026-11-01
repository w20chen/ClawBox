import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name == 'nt' or os.geteuid() != 0,
                               reason='isolated Bash harness requires Linux root')
ROOT = Path(__file__).resolve().parents[1]


def test_proxy_start_reuse_and_config_mismatch(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    docker = bin_dir / 'docker'
    docker.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(os.environ['CACHE_TEST_ROOT']);args=sys.argv[1:]
with (root/'docker.jsonl').open('a') as f:f.write(json.dumps(args)+'\\n')
if args[0]=='ps' and (root/'running').exists():print('clawbox-pypi-cache')
elif args[0]=='run':(root/'running').touch();print('fixture-container')
elif args[0]=='inspect':print('host' if 'NetworkMode' in args[2] else 'HTTPS_PROXY=http://127.0.0.1:8118')
''')
    docker.chmod(0o755)
    curl = bin_dir / 'curl'
    curl.write_text('#!/bin/sh\nexit 0\n')
    curl.chmod(0o755)
    env = dict(os.environ, CACHE_TEST_ROOT=str(tmp_path), PATH=str(bin_dir)+':'+os.environ['PATH'])
    command = ['bash', str(ROOT/'scripts/clawbox-pypi-cache'), 'start',
               '--bind-address', '192.0.2.1', '--data-root', str(tmp_path/'cache'),
               '--upstream-proxy', 'http://127.0.0.1:8118']
    rejected = subprocess.run(command, env=env, capture_output=True, text=True)
    assert rejected.returncode == 2 and '--network-host' in rejected.stderr
    command += ['--network-host']
    for _ in range(2):
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert 'HTTP endpoint is ready' in result.stdout
    calls = list(map(json.loads, (tmp_path/'docker.jsonl').read_text().splitlines()))
    runs = [args for args in calls if args[0] == 'run']
    assert len(runs) == 1
    assert '--network' in runs[0] and 'host' in runs[0]
    assert '-p' not in runs[0]
    assert 'HTTPS_PROXY=http://127.0.0.1:8118' in runs[0]
    assert 'CLAWBOX_PYPI_BIND_ADDRESS=192.0.2.1' in runs[0]
    assert str(tmp_path/'cache/data')+':/data' in runs[0]
    command[command.index('http://127.0.0.1:8118')] = 'http://127.0.0.1:8119'
    rejected = subprocess.run(command, env=env, capture_output=True, text=True)
    assert rejected.returncode == 1 and 'backup name' in rejected.stderr
