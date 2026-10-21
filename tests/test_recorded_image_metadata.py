import pytest
import hashlib

from clawbox.images.recorded import apply_metadata_whiteouts, download_layer, installed_metadata_path
from clawbox.images.swerebench import _registry_session


@pytest.mark.parametrize("path,expected", [
    ("opt/conda/envs/testbed/lib/python3.9/site-packages/pytest-8.3.5.dist-info/METADATA", True),
    ("opt/conda/envs/testbed/lib/python3.9/site-packages/astor.egg-info/PKG-INFO", True),
    ("opt/conda/envs/testbed/lib/python3.9/site-packages/setuptools/_vendor/packaging-24.2.dist-info/METADATA", False),
    ("opt/conda/lib/python3.12/site-packages/pip-25.0.dist-info/METADATA", False),
])
def test_only_installed_task_distributions_are_inventory_entries(path, expected):
    assert installed_metadata_path(path) is expected


def test_explicit_registry_proxy_overrides_ambient_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "socks5://unrelated-proxy:1080")
    with _registry_session("http://127.0.0.1:17897") as session:
        settings = session.merge_environment_settings("https://registry-1.docker.io/v2/", {}, False, None, None)
        assert settings["proxies"]["https"] == "http://127.0.0.1:17897"


@pytest.mark.parametrize('resume_status', [200, 206])
def test_direct_mirror_resumes_or_restarts_and_verifies(monkeypatch, tmp_path, resume_status):
    payload = b'official-image-layer'
    digest = 'sha256:' + hashlib.sha256(payload).hexdigest()
    destination = tmp_path / 'layer.tgz'
    destination.with_suffix('.part').write_bytes(payload[:8])
    monkeypatch.setenv('CLAWBOX_REGISTRY_MIRRORS', 'https://mirror.example')

    class Response:
        status_code = resume_status
        headers = {'Content-Range': f'bytes 8-{len(payload)-1}/{len(payload)}'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size):
            yield payload[8:] if resume_status == 206 else payload

    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            assert self.trust_env is False
            assert url == f'https://mirror.example/v2/swerebench/example/blobs/{digest}'
            assert kwargs['headers'] == {'Range': 'bytes=8-'}
            return Response()

    monkeypatch.setattr('requests.Session', Session)
    download_layer('swerebench/example:latest', digest, destination, 'http://unused-proxy')
    assert destination.read_bytes() == payload
    assert not destination.with_suffix('.part').exists()


def test_bad_mirror_blob_is_rejected_before_next_mirror(monkeypatch, tmp_path):
    payload = b'official-layer'
    digest = 'sha256:' + hashlib.sha256(payload).hexdigest()
    monkeypatch.setenv('CLAWBOX_REGISTRY_MIRRORS', 'https://bad.example,https://good.example')
    monkeypatch.setattr('clawbox.images.recorded.time.sleep', lambda _: None)
    requested = []

    class Response:
        status_code = 200
        headers = {}
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size): yield self.data

    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            requested.append(url)
            assert kwargs['headers'] == {}
            return Response(b'wrong layer' if 'bad.example' in url else payload)

    monkeypatch.setattr('requests.Session', Session)
    destination = tmp_path / 'layer.tgz'
    download_layer('swerebench/example:latest', digest, destination, '')
    assert len(requested) == 2
    assert destination.read_bytes() == payload


def test_oci_metadata_deletions_remove_only_inherited_targets():
    records = {'conda-meta/old.json': ('old', '1'),
               'site-packages/demo.dist-info/METADATA': ('demo', '1'),
               'site-packages/keep.dist-info/METADATA': ('keep', '2')}
    apply_metadata_whiteouts(records, ['conda-meta/.wh.old.json', 'site-packages/.wh.demo.dist-info'])
    assert records == {'site-packages/keep.dist-info/METADATA': ('keep', '2')}
    apply_metadata_whiteouts(records, ['site-packages/.wh..wh..opq'])
    assert records == {}
