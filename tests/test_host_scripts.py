"""Linux terminal entrypoint: configuration, quoting, and safe failures."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='Linux Bash entrypoints')
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('script', ['bootstrap-python.sh', 'build-cubesandbox-release.sh',
                                   'register-machine-templates.sh', 'export-machine-assets.sh'])
def test_helpers_explain_usage_without_a_configured_host(tmp_path, script):
    env = dict(os.environ, HOME=str(tmp_path))
    env.pop('CLAWBOX_MACHINE_ENV', None)
    result = subprocess.run(['bash', str(ROOT/'scripts'/script), '--help'], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0 and 'Usage:' in result.stdout
    assert not list(tmp_path.iterdir())


def test_fresh_shell_loads_machine_and_preserves_arguments(tmp_path):
    project = tmp_path / 'project with spaces'
    (project / 'scripts').mkdir(parents=True)
    shutil.copyfile(ROOT / 'scripts/clawbox', project / 'scripts/clawbox')
    binary = project / '.venv/bin'
    binary.mkdir(parents=True)
    python = binary / 'python'
    python.write_text('''#!/usr/bin/env python3
import json, os, sys
if sys.argv[1] != '-c':
    print(json.dumps({'args': sys.argv[1:], 'output': os.environ.get('CLAWBOX_OUTPUT_ROOT'),
                      'home': os.environ['HOME'], 'path': os.environ['PYTHONPATH']}))
''')
    python.chmod(0o755)
    home = tmp_path / 'home'
    machine = home / '.config/clawbox/machine.env'
    machine.parent.mkdir(parents=True)
    machine.write_text("CLAWBOX_OUTPUT_ROOT='/data/results with spaces'\n")
    env = {key: value for key, value in os.environ.items()
           if key not in ('CLAWBOX_MACHINE_ENV', 'CLAWBOX_PYTHON')}
    env.update(HOME=str(home), CLAWBOX_OUTPUT_ROOT='/stale')
    command = ['bash', str(project / 'scripts/clawbox')]
    result = subprocess.run(command + ['experiment', 'configure', 'a b.yaml'], env=env,
                            check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    assert payload['args'] == ['-m', 'clawbox.cli', 'experiment', 'configure', 'a b.yaml']
    assert payload['output'] == '/data/results with spaces'
    assert payload['home'] == str(home)
    assert payload['path'].split(':')[0] == str(project)
    missing = subprocess.run(command + ['--machine-env', str(tmp_path / 'missing'), 'experiment', '--help'],
                             env=env, capture_output=True, text=True)
    assert missing.returncode == 2 and 'not found' in missing.stderr
    machine.unlink()
    explicit_default = subprocess.run(command + ['--machine-env', str(machine), 'experiment', '--help'],
                                      env=env, capture_output=True, text=True)
    assert explicit_default.returncode == 2 and 'not found' in explicit_default.stderr


def test_bootstrap_rejects_invalid_source_before_creating_environment(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path))
    env.pop('CLAWBOX_MACHINE_ENV', None)
    target = tmp_path / 'fresh'
    result = subprocess.run(['bash', str(ROOT / 'scripts/bootstrap-python.sh'),
                             '--venv', str(target), '--cube-source', str(tmp_path / 'absent')],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0 and 'Invalid --cube-source' in result.stderr
    assert not target.exists()


def test_release_build_requires_exported_bundle_before_docker(tmp_path):
    result = subprocess.run(['bash', str(ROOT / 'scripts/build-cubesandbox-release.sh'),
                             '--assets', str(tmp_path / 'missing'), '--source', str(tmp_path / 'source')],
                            capture_output=True, text=True)
    assert result.returncode == 2 and 'SHA256SUMS' in result.stderr
    assert not (tmp_path / 'source').exists()


def test_release_cli_overrides_saved_build_options_without_overwriting_them(tmp_path):
    import hashlib
    project = tmp_path / 'project'
    (project / 'scripts').mkdir(parents=True)
    shutil.copyfile(ROOT / 'scripts/build-cubesandbox-release.sh', project / 'scripts/build.sh')
    prepare = project / 'deploy/cubesandbox'
    prepare.mkdir(parents=True)
    (prepare / 'prepare-semantic-source.sh').write_text('exit 0\n')
    source = tmp_path / 'source'
    one_click = source / 'deploy/one-click'
    one_click.mkdir(parents=True)
    saved = one_click / 'build.env'
    saved.write_text('ONE_CLICK_BUILD_JOBS=99\nONE_CLICK_CUBE_KERNEL_VMLINUX=/wrong\n')
    before = saved.read_bytes()
    (one_click / 'build-release-bundle-builder.sh').write_text(
        'source "$ONE_CLICK_BUILD_ENV_FILE"\nprintf "chosen=%s,%s\\n" "$ONE_CLICK_BUILD_JOBS" "$ONE_CLICK_CUBE_KERNEL_VMLINUX"\n')
    assets = tmp_path / 'assets with spaces'
    (assets / 'kernel').mkdir(parents=True)
    (assets / 'kernel/vmlinux-bm').write_bytes(b'kernel')
    (assets / 'SHA256SUMS').write_text(hashlib.sha256(b'kernel').hexdigest() + '  kernel/vmlinux-bm\n')
    binary = tmp_path / 'bin'
    binary.mkdir()
    for name in ('docker', 'make'):
        executable = binary / name
        executable.write_text('#!/bin/sh\nprintf "proxy=%s args=%s\\n" "$GOPROXY" "$BUILDER_BUILD_ARGS"\nexit 0\n')
        executable.chmod(0o755)
    env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'], BUILDX_CONFIG=str(tmp_path / 'buildx'))
    result = subprocess.run(['bash', str(project / 'scripts/build.sh'), '--source', str(source),
                             '--assets', str(assets), '--jobs', '2', '--mirror', 'cn',
                             '--go-proxy', 'https://modules.example.test,direct'], env=env, check=True,
                            capture_output=True, text=True)
    assert f'chosen=2,{assets}/kernel/vmlinux-bm' in result.stdout
    assert saved.read_bytes() == before
    assert 'proxy=https://modules.example.test,direct args=' in result.stdout
    assert '--build-arg GOPROXY' in result.stdout


def test_export_can_use_applied_profile_without_machine_images(tmp_path):
    kernel = tmp_path / 'kernel'
    kernel.mkdir()
    for name in ('vmlinux-bm', 'version', 'version.json'):
        (kernel / name).write_text('kernel')
    profile = tmp_path / 'host.json'
    profile.write_text(json.dumps({'runtime': {'source_image_reference': 'runtime@sha256:abc'},
                                   'sandbox': {'source_image_reference': 'tool@sha256:def'}}))
    binary = tmp_path / 'bin'
    binary.mkdir()
    docker = binary / 'docker'
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\nif [ "$1" = save ]; then touch "$3"; fi\n')
    docker.chmod(0o755)
    env = dict(os.environ, HOME=str(tmp_path), CLAWBOX_KERNEL_ROOT=str(kernel),
               PATH=str(binary) + ':' + os.environ['PATH'], DOCKER_LOG=str(tmp_path / 'docker.log'))
    env.pop('CLAWBOX_MACHINE_ENV', None)
    result = subprocess.run(['bash', str(ROOT / 'scripts/export-machine-assets.sh'), str(tmp_path / 'export'),
                             '--profile', str(profile)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'tag runtime@sha256:abc' in (tmp_path / 'docker.log').read_text()
    assert 'tag tool@sha256:def' in (tmp_path / 'docker.log').read_text()
