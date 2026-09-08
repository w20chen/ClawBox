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
def test_public_wrapper_has_no_cluster_fallback():
    source = (ROOT / 'scripts/clawbox').read_text()
    assert 'study.py' not in source
    assert '-m clawbox.cli' in source
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
