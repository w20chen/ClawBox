"""Recover task state and environment evidence from official SWE-ReBench layers.

This module never clones or resets task repositories. Recovered metadata is
evidence for a native rebuild; it is not a claim of cross-architecture identity.
"""
from __future__ import annotations

import email.parser
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import threading
import time
from typing import Any

from .arm64 import safe_tag, write_mapping
from .swerebench import _registry_session, _testbed_member_filter, _docker_hub_reference

_REGISTRY_REQUESTS = threading.BoundedSemaphore(8)


def canonical_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_metadata_path(name: str) -> bool:
    """Exclude vendored libraries' metadata below an installed distribution."""
    return bool(re.fullmatch(
        r'opt/conda/envs/testbed/lib/python[^/]+/site-packages/[^/]+\.(?:dist|egg)-info/(?:METADATA|PKG-INFO)', name))


def apply_metadata_whiteouts(records: dict, names: list[str]) -> None:
    """Apply OCI deletions to metadata inherited from earlier layers."""
    for name in names:
        parent, _, basename = name.rpartition('/')
        if not basename.startswith('.wh.'):
            continue
        target = parent + '/' + basename.removeprefix('.wh.')
        if basename == '.wh..wh..opq':
            target = parent
        for key in list(records):
            if key == target or key.startswith(target + '/'):
                del records[key]


def download_layer(image: str, digest: str, destination: Path, proxy: str) -> None:
    """Use configured direct mirrors, then Docker Hub; verify every blob.

    CLAWBOX_REGISTRY_MIRRORS is a comma-separated list of HTTPS origins.
    Mirrors receive no Docker Hub credentials. The digest must come from the
    independently resolved official manifest, never from the mirror itself.
    """
    from urllib.parse import urlsplit
    import requests

    mirrors = [value.strip().rstrip('/') for value in
               os.environ.get('CLAWBOX_REGISTRY_MIRRORS', '').split(',') if value.strip()]
    for mirror in mirrors:
        parsed = urlsplit(mirror)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('registry mirrors must be HTTPS origins without credentials')
    routes = mirrors + [None, None]
    partial = destination.with_suffix('.part')
    if destination.exists():
        with destination.open('rb') as stream:
            if 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest() == digest:
                return
        destination.unlink()
    for attempt in range(max(4, len(routes))):
        try:
            repository, _ = _docker_hub_reference(image)
            url = f"https://registry-1.docker.io/v2/{repository}/blobs/{digest}"
            with ExitStack() as stack:
                direct = stack.enter_context(requests.Session())
                direct.trust_env = False
                offset = partial.stat().st_size if partial.exists() else 0
                range_headers = {'Range': f'bytes={offset}-'} if offset else {}
                mirror = routes[min(attempt, len(routes) - 1)]
                if mirror:
                    response = direct.get(f'{mirror}/v2/{repository}/blobs/{digest}',
                                          headers=range_headers, stream=True, timeout=(15, 90))
                else:
                    session = stack.enter_context(_registry_session(proxy))
                    with _REGISTRY_REQUESTS:
                        token = session.get('https://auth.docker.io/token', params={
                            'service': 'registry.docker.io', 'scope': f'repository:{repository}:pull'}, timeout=60)
                    with token:
                        token.raise_for_status()
                        headers = {**range_headers, 'Authorization': 'Bearer ' + token.json()['token']}
                    with _REGISTRY_REQUESTS:
                        response = session.get(url, headers=headers, stream=True,
                                               allow_redirects=False, timeout=(60, 600))
                    if response.is_redirect:
                        location = response.headers['Location']
                        response.close()
                        # Never forward the registry bearer token to the CDN.
                        try:
                            response = direct.get(location, headers=range_headers,
                                                  stream=True, timeout=(8, 60))
                            response.raise_for_status()
                        except requests.RequestException:
                            response.close()
                            response = session.get(location, headers=range_headers,
                                                   stream=True, timeout=(60, 600))
                with response:
                    response.raise_for_status()
                    if response.status_code != 206:
                        offset = 0
                    elif not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                        raise RuntimeError('registry returned an inconsistent byte range')
                    hasher = hashlib.sha256()
                    if offset:
                        with partial.open('rb') as previous:
                            while chunk := previous.read(4 * 1024 * 1024):
                                hasher.update(chunk)
                    with partial.open('ab' if offset else 'wb') as output:
                        for chunk in response.iter_content(4 * 1024 * 1024):
                            if chunk:
                                output.write(chunk)
                                hasher.update(chunk)
            if 'sha256:' + hasher.hexdigest() != digest:
                partial.unlink(missing_ok=True)
                raise RuntimeError(f'layer checksum mismatch: {digest}')
            partial.replace(destination)
            return
        except Exception:
            if attempt == max(4, len(routes)) - 1:
                raise
            time.sleep(2 ** attempt)


def prepare_task(source: dict[str, Any], root: Path, proxy: str,
                 min_free_gib: int = 150) -> dict[str, Any]:
    task = source['instance_id']
    directory = root.resolve() / safe_tag(task)
    if directory.resolve().parent != root.resolve():
        raise ValueError('task workspace escapes preparation root')
    receipt_path = directory / 'prepared.json'
    if receipt_path.is_file():
        previous = json.loads(receipt_path.read_text())
        if (previous['manifest_digest'] == source['manifest_digest'] and previous.get('inventory_version') == 2
                and (len(previous.get('layers', [])) == 1 or previous.get('metadata_overlay_version') == 1)):
            return previous
    if shutil.disk_usage(root).free < min_free_gib * 1024**3:
        raise RuntimeError('insufficient disk space for a new task layer')
    directory.mkdir(exist_ok=True)
    for stale in ('unpack', 'testbed'):
        if (directory / stale).exists():
            shutil.rmtree(directory / stale)
    candidates = []
    try:
        # Keep newer layers until a complete checkout is found, then apply them
        # in manifest order. HEAD alone is checked with git fsck below.
        for index, layer in reversed(list(enumerate(source['manifest']['layers']))):
            blob = directory / f'layer-{index}.tgz'
            download_layer(source['pinned_image'], layer['digest'], blob, proxy)
            candidates.append((index, layer, blob))
            with tarfile.open(blob) as archive:
                if any(m.name.removeprefix('./') == 'testbed/.git/HEAD' for m in archive):
                    break
        else:
            raise RuntimeError('official image contains no /testbed Git checkout')
        unpack = directory / 'unpack'
        unpack.mkdir()
        packages: dict[str, str] = {}
        conda: dict[str, str] = {}
        package_records: dict[str, tuple[str, str]] = {}
        conda_records: dict[str, tuple[str, str]] = {}
        evidence: dict[str, str] = {}
        editable = []
        for index, layer, blob in sorted(candidates):
            with tarfile.open(blob) as archive:
                members = []
                layer_members = archive.getmembers()
                names = [m.name.removeprefix('./') for m in layer_members]
                apply_metadata_whiteouts(package_records, names)
                apply_metadata_whiteouts(conda_records, names)
                for member in layer_members:
                    name = member.name
                    while name.startswith('./'):
                        name = name[2:]
                    if name == 'testbed' or name.startswith('testbed/'):
                        member.name = name
                        if '/.wh.' in name:
                            # Fail explicitly rather than treating Docker deletion
                            # markers as ordinary checkout files.
                            raise RuntimeError(f'testbed overlay whiteout requires reconstruction: {name}')
                        members.append(member)
                    if '/.wh.' in name or not member.isfile() or member.size > 4 * 1024 * 1024:
                        continue
                    if installed_metadata_path(name):
                        data = archive.extractfile(member).read().decode('utf-8', errors='replace')
                        metadata = email.parser.Parser().parsestr(data, headersonly=True)
                        if metadata.get('Name') and metadata.get('Version'):
                            package_records[name] = (canonical_package(metadata['Name']), metadata['Version'])
                    elif name.startswith('opt/conda/envs/testbed/conda-meta/') and name.endswith('.json'):
                        data = json.load(archive.extractfile(member))
                        conda_records[name] = (data['name'], data['version'])
                    elif '/envs/testbed/' in name and name.endswith('.egg-link'):
                        editable.append({'name': name, 'target': archive.extractfile(member).read().decode().strip()})
                    elif name in ('root/.bashrc', 'root/.profile', 'tmp/_build_script.sh',
                                  'swebench_instance_requirements/requirements.txt', 'etc/profile.d/conda.sh'):
                        evidence[name] = archive.extractfile(member).read().decode('utf-8', errors='replace')
                archive.extractall(unpack, members=members, filter=_testbed_member_filter)
            blob.unlink()
        packages = dict(package_records.values())
        conda = dict(conda_records.values())
        workspace = unpack / 'testbed'
        subprocess.run(['git', '-C', str(workspace), 'fsck', '--connectivity-only', '--no-dangling'],
                       check=True, capture_output=True, text=True)
        head = subprocess.check_output(['git', '-C', str(workspace), 'rev-parse', 'HEAD'], text=True).strip()
        status = subprocess.check_output(['git', '-C', str(workspace), 'status', '--porcelain'], text=True)
        if not conda.get('python') or not packages:
            raise RuntimeError('task layer lacks authoritative Python/package metadata')
        # Editable project identities are installed from the recovered checkout.
        for link in editable:
            if link['target'].splitlines()[0] == '/testbed':
                packages.pop(canonical_package(Path(link['name']).stem), None)
        project_packages = {}
        if editable:
            for metadata_path in workspace.glob('*.egg-info/PKG-INFO'):
                metadata = email.parser.Parser().parsestr(metadata_path.read_text(errors='replace'), headersonly=True)
                if metadata.get('Name'):
                    key = canonical_package(metadata['Name'])
                    project_packages[key] = metadata.get('Version')
                    packages.pop(key, None)
        workspace.rename(directory / 'testbed')
        unpack.rmdir()
        receipt = {
            'instance_id': task, 'manifest_digest': source['manifest_digest'],
            'original_image': source['original_image'], 'pinned_image': source['pinned_image'],
            'workspace_git_head': head, 'workspace_git_status': status,
            'layers': [layer['digest'] for _, layer, _ in sorted(candidates)],
            'python_version': conda['python'], 'packages': packages,
            'project_packages': project_packages, 'inventory_version': 2,
            'metadata_overlay_version': 1,
            'conda_packages': conda, 'editable': editable, 'image_files': evidence,
            'source_config': source['config']['config'],
            'recording_time_digest_available': source['recording_time_digest_available'],
            'status': 'source-prepared',
        }
        write_mapping(receipt_path, receipt)
        return receipt
    except BaseException:
        # Preserve resumable downloads across network outages. Extraction is
        # always retried from a clean destination after the blob hash verifies.
        for child in directory.iterdir():
            if re.fullmatch(r'layer-\d+\.(?:part|tgz)', child.name) and child.is_file():
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        raise
