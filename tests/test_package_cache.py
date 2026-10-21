import hashlib
from types import SimpleNamespace

from clawbox.images.package_cache import Cache, index_html


def test_index_uses_official_hash_and_preserves_constraints():
    digest = 'a' * 64
    data = {'files': [
        {'filename': 'demo.whl', 'url': 'https://files.pythonhosted.org/packages/aa/demo.whl',
         'hashes': {'sha256': digest}, 'requires-python': '>=3.9', 'yanked': 'broken release'},
        {'filename': 'untrusted.whl', 'url': 'https://unrelated.example/demo.whl',
         'hashes': {'sha256': digest}},
    ]}
    result = index_html(data).decode()
    assert f'/files/{digest}/packages/aa/demo.whl#sha256={digest}' in result
    assert 'data-requires-python="&gt;=3.9"' in result
    assert 'data-yanked="broken release"' in result
    assert 'untrusted.whl' not in result


def test_corrupt_mirror_artifact_falls_back_to_official_and_caches(monkeypatch, tmp_path):
    payload = b'verified official wheel bytes'
    digest = hashlib.sha256(payload).hexdigest()
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
            assert self.trust_env is False
            requested.append(url)
            return Response(b'corrupt' if 'aliyun' in url else payload)

    monkeypatch.setattr('clawbox.images.package_cache.requests.Session', Session)
    monkeypatch.setattr('clawbox.images.package_cache.time.sleep', lambda _: None)
    monkeypatch.setattr('clawbox.images.package_cache.shutil.disk_usage', lambda _: SimpleNamespace(free=2**40))
    cache = Cache(tmp_path)
    first = cache.artifact(digest, '/packages/aa/demo.whl')
    assert first.read_bytes() == payload
    assert len(requested) == 2
    assert cache.artifact(digest, '/packages/aa/demo.whl') == first
    assert len(requested) == 2
