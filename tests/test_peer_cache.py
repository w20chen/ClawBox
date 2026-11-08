from contextlib import nullcontext
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawbox.experiments import peer_cache


def test_reset_cache_public_cli_defaults_to_preview():
    from clawbox.cli import parser
    args = parser().parse_args(['experiment', 'snapshot-storage', 'reset-cache', 'host.yaml'])
    assert not args.apply and args.config == Path('host.yaml')


@pytest.mark.skipif(os.name != 'posix', reason='Linux file identity and mincore')
def test_inode_inventory_excludes_symlinked_warm_and_deduplicates_hardlinks(tmp_path):
    root, warm = tmp_path/'ssd', tmp_path/'warm'
    root.mkdir(); warm.mkdir()
    (root/'base').write_bytes(b'base')
    os.link(root/'base', root/'alias')
    (warm/'keep').write_bytes(b'warm')
    (root/'outside').symlink_to(warm/'keep')
    (root/'outside-dir').symlink_to(warm, target_is_directory=True)
    rows = peer_cache._files(root)
    assert len(rows) == 1 and rows[0]['size'] == 4


@pytest.mark.skipif(os.name != 'posix', reason='Linux advisory eviction and mincore')
def test_preview_does_not_advise_and_apply_records_observed_residency(tmp_path, monkeypatch):
    from clawbox.experiments import base_cache, snapshot_host
    roots = [tmp_path/'node0', tmp_path/'node1']
    warm = tmp_path/'warm'
    warm.mkdir()
    (warm/'untouched').write_bytes(b'warm')
    for root in roots:
        root.mkdir()
        (root/'base').write_bytes(b'x' * 8192)
    images = [tmp_path/'node0.img', tmp_path/'node1.img']
    for image in images:
        image.write_bytes(b'y' * 16384)
    unrelated = tmp_path/'unrelated'
    unrelated.write_bytes(b'preserved')
    state = tmp_path/'state.json'
    state.write_text(json.dumps({'configuration': {'nodes': {'node0': {}, 'node1': {}}},
        'loops': {f'node{i}': {'image': str(images[i]), 'mount': str(root)} for i, root in enumerate(roots)}}))
    spec = SimpleNamespace(model_dump=lambda **_: {'nodes': {
        'node0': {'direct_io': False}, 'node1': {'direct_io': False}}}, receipt='owned-receipt',
        shared_dram=SimpleNamespace(root=str(warm)),
        nodes={f'node{i}': SimpleNamespace(local_mount=str(root), image_path=str(images[i]),
            backing_mount=str(tmp_path)) for i, root in enumerate(roots)})
    monkeypatch.setattr(snapshot_host, '_state_path', lambda _: state)
    monkeypatch.setattr(snapshot_host, 'storage_configuration', lambda _: {})
    monkeypatch.setattr(snapshot_host, 'validate_peer_storage', lambda _: {'receipt': 'owned-receipt'})
    monkeypatch.setattr(base_cache, '_maintenance', nullcontext)
    monkeypatch.setattr(base_cache, '_assert_idle', lambda: None)
    monkeypatch.setattr(peer_cache, '_assert_no_receivers', lambda: None)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    advice = []
    real = os.posix_fadvise
    def advise(fd, *args):
        advice.append(str(Path(f'/proc/self/fd/{fd}').resolve()))
        return real(fd, *args)
    monkeypatch.setattr(os, 'posix_fadvise', advise)
    preview = peer_cache.reset_cache(spec)
    assert not advice and not preview['applied']
    applied = peer_cache.reset_cache(spec, execute=True)
    assert len(advice) == 4 and applied['applied']
    assert all(row['requested_evict_bytes'] == 8192 and row['resident_pages_after'] >= 0
               for row in applied['nodes'].values())
    assert (warm/'untouched').read_bytes() == b'warm'
    assert all(row['requested_evict_bytes'] == 16384 and row['resident_pages_after'] >= 0
               for row in applied['backing_images'].values())
    assert str(unrelated) not in advice and str(warm/'untouched') not in advice
    assert advice[-2:] == [str(image) for image in images]
    assert unrelated.read_bytes() == b'preserved'
