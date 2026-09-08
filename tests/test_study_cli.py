"""Daily commands must validate inputs before launching experiment work."""
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('study_wrapper', ROOT / 'scripts/study.py')
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr(sys, 'argv', ['study', *map(str, arguments)])
    study.main()


def test_init_uses_machine_settings_and_does_not_overwrite(tmp_path, monkeypatch):
    trace = tmp_path / 'trace.jsonl'
    trace.write_text('{}\n')
    output = tmp_path / 'study.yaml'
    settings = dict(CUBE_NODE='node', CLAWBOX_RUNTIME_TEMPLATE='runtime',
                    CLAWBOX_TOOL_TEMPLATE='tool', CLAWBOX_RUNTIME_IMAGE='registry/runtime@sha256:' + 'a' * 64,
                    CLAWBOX_TOOL_IMAGE='registry/tool@sha256:' + 'b' * 64,
                    CLAWBOX_WARM_ROOT='/data/warm', CLAWBOX_COLD_ROOT='/data/cold')
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    invoke(monkeypatch, 'init', '--trace', trace, '--output', output)
    value = yaml.safe_load(output.read_text())
    assert value['workload']['input'] == str(trace.resolve())
    assert value['runtime']['template_id'] == 'runtime'
    assert value['resources']['target_node'] == 'node'
    with pytest.raises(FileExistsError):
        invoke(monkeypatch, 'init', '--trace', trace, '--output', output)


@pytest.mark.parametrize('command', ['start', 'setup-memory'])
def test_busy_pool_cannot_be_reconfigured_or_started(tmp_path, monkeypatch, command):
    (tmp_path / 'cgroup.events').write_text('populated 1\n')
    monkeypatch.setattr(study, 'check', lambda path: None)
    monkeypatch.setattr(study, 'read_spec', lambda path: {'resources': {'local_memory_cgroup': str(tmp_path)}})
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, command, '--spec', tmp_path / 'study.yaml')
    assert error.value.code == 2


def test_start_detaches_and_keeps_a_log(tmp_path, monkeypatch):
    (tmp_path / 'cgroup.events').write_text('populated 0\n')
    monkeypatch.setattr(study, 'check', lambda path: None)
    monkeypatch.setattr(study, 'read_spec', lambda path: {'resources': {'local_memory_cgroup': str(tmp_path)}})
    launched = []
    def launch(command, **kwargs):
        launched.append((command, kwargs))
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(study.subprocess, 'Popen', launch)
    invoke(monkeypatch, 'start', '--spec', tmp_path / 'study.yaml', '--name', 'check-01', '--output-root', tmp_path)
    command, options = launched[0]
    assert command[-2:] == ['--steps', '5']
    assert options['start_new_session'] is True
    assert options['stdin'] == study.subprocess.DEVNULL
    assert (tmp_path / 'check-01.log').exists()
    with pytest.raises(FileExistsError):
        invoke(monkeypatch, 'start', '--spec', tmp_path / 'study.yaml', '--name', 'check-01', '--output-root', tmp_path)


def test_public_wrapper_has_no_cluster_fallback():
    source = (ROOT / 'scripts/clawbox').read_text()
    assert 'study.py' in source
    assert 'cube-host-doctor.py' in source
    assert 'kubectl' not in source
    assert 'clawbox-host.sh' not in source


def test_document_links_resolve():
    documents = [ROOT / 'README.md', ROOT / 'scripts/README.md', ROOT / 'deploy/README.md', *ROOT.glob('docs/*.md')]
    for document in documents:
        for target in re.findall(r'\]\(([^)]+)\)', document.read_text(encoding='utf-8')):
            if '://' not in target and not target.startswith('#'):
                assert (document.parent / target.split('#')[0]).exists(), (document, target)


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux installation helper')
def test_export_packages_kernel_and_selected_images(tmp_path):
    kernel = tmp_path / 'kernel'
    kernel.mkdir()
    for filename in ('vmlinux-bm', 'version', 'version.json'):
        (kernel / filename).write_text('fixture')
    profile = tmp_path / 'machine.env'
    profile.write_text('CLAWBOX_RUNTIME_IMAGE=registry/runtime\nCLAWBOX_TOOL_IMAGE=registry/tool\n')
    binary = tmp_path / 'bin'
    binary.mkdir()
    docker = binary / 'docker'
    docker.write_text('#!/bin/sh\nif [ "$1" = save ]; then touch "$3"; fi\n')
    docker.chmod(0o755)
    output = tmp_path / 'export'
    env = {**os.environ, 'CLAWBOX_MACHINE_ENV': str(profile), 'CLAWBOX_KERNEL_ROOT': str(kernel),
           'PATH': str(binary) + ':' + os.environ['PATH']}
    command = ['bash', str(ROOT / 'scripts/export-machine-assets.sh'), str(output)]
    subprocess.run(command, env=env, check=True)
    assert (output / 'guest-images.tar').is_file()
    assert (output / 'kernel/vmlinux-bm').read_text() == 'fixture'
    assert subprocess.run(command, env=env).returncode != 0
