import os
from pathlib import Path
import time

import pytest

from clawbox.experiments.disk_cleanup import clean_dumps


def dump(path):
    with path.open('wb') as stream:
        stream.write(b'KDUMP   ')
        stream.truncate(2 * 1024**2)
    old = time.time() - 48 * 3600
    os.utime(path, (old, old))


def test_cleanup_only_old_verified_dumps_and_preview_is_read_only(tmp_path):
    old = tmp_path / 'vmcore'
    dump(old)
    recent_dir = tmp_path / 'recent'
    recent_dir.mkdir()
    recent = recent_dir / 'vmcore'
    dump(recent)
    os.utime(recent, None)
    result = tmp_path / 'results.json'
    dump(result)
    impostor = tmp_path / 'vmcore.not-a-dump'
    impostor.write_bytes(b'X' * (2 * 1024**2))
    past = time.time() - 48 * 3600
    os.utime(impostor, (past, past))
    preview = clean_dumps([tmp_path], min_size_mib=1)
    assert [row['path'] for row in preview['files']] == [str(old)]
    assert old.exists() and not preview['files'][0]['deleted']
    applied = clean_dumps([tmp_path], apply=True, min_size_mib=1)
    assert not old.exists() and applied['files'][0]['deleted']
    assert recent.exists() and result.exists() and impostor.exists()
    assert clean_dumps([tmp_path], apply=True, min_size_mib=1)['files'] == []


@pytest.mark.skipif(os.name == 'nt', reason='Linux symlink permissions')
def test_cleanup_does_not_follow_links_or_delete_hardlinked_files(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    dump(actual / 'vmcore')
    linked = tmp_path / 'linked'
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError):
        clean_dumps([linked], apply=True, min_size_mib=1)
    scan = tmp_path / 'scan'
    scan.mkdir()
    (scan / 'vmcore').symlink_to(actual / 'vmcore')
    (scan / 'dir').symlink_to(actual, target_is_directory=True)
    os.link(actual / 'vmcore', scan / 'vmcore.copy')
    assert clean_dumps([scan], apply=True, min_size_mib=1)['files'] == []
    assert (actual / 'vmcore').exists()
