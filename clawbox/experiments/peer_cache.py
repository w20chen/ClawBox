"""Narrow peer-SSD page-cache reset between matched experiment arms."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import stat
import time
from types import SimpleNamespace


def _assert_no_receivers():
    active = []
    for entry in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            argv = entry.read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        if b'clawbox.experiments.peer_transport' in argv and b'fetch' in argv:
            active.append(int(entry.parent.name))
    if active:
        raise RuntimeError(f'peer transfers remain active (PIDs {active}); stop/wait for the run before reset-cache')


def _files(root: Path):
    root = root.resolve(strict=True)
    device = root.stat().st_dev
    seen = set()
    result = []
    for directory, children, names in os.walk(root, followlinks=False):
        children[:] = [name for name in children
                       if not (Path(directory)/name).is_symlink()
                       and (Path(directory)/name).stat().st_dev == device]
        for name in names:
            path = Path(directory)/name
            info = path.lstat()
            key = info.st_dev, info.st_ino
            if not stat.S_ISREG(info.st_mode) or info.st_dev != device or key in seen:
                continue
            if not path.resolve(strict=True).is_relative_to(root):
                raise RuntimeError(f'file escapes owned SSD root: {path}')
            seen.add(key)
            result.append({'path': str(path), 'dev': info.st_dev, 'ino': info.st_ino,
                           'size': info.st_size, 'allocated_bytes': info.st_blocks*512})
    return result


def _resident_pages(fd: int, size: int) -> int:
    """mincore over non-accessed mappings: observe cache without faulting data."""
    if size == 0:
        return 0
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_longlong]
    libc.mmap.restype = ctypes.c_void_p
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    libc.mincore.restype = ctypes.c_int
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    libc.munmap.restype = ctypes.c_int
    page_size = os.sysconf('SC_PAGE_SIZE')
    total = 0
    for offset in range(0, size, 1024**3):
        length = min(1024**3, size-offset)
        address = libc.mmap(None, length, 0, 1, fd, offset)  # PROT_NONE, MAP_SHARED
        if address == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_errno(), 'mmap for non-faulting mincore observation failed')
        try:
            pages = (length+page_size-1)//page_size
            vector = (ctypes.c_ubyte * pages)()
            if libc.mincore(address, length, vector) != 0:
                raise OSError(ctypes.get_errno(), 'mincore failed; no eviction claim can be made')
            # Terabyte SSD pools contain billions of 4KiB pages. Count the
            # exact same low bits in C instead of iterating ctypes in Python.
            total += bytes(vector).translate(bytes(i & 1 for i in range(256))).count(b'\x01')
        finally:
            if libc.munmap(address, length) != 0:
                raise OSError(ctypes.get_errno(), 'mincore mapping release failed')
    return total


def _reset_files(rows, *, execute):
    before = after = 0
    for row in rows:
        fd = os.open(row['path'], os.O_RDONLY | os.O_NOFOLLOW)
        try:
            current = os.fstat(fd)
            if (current.st_dev, current.st_ino, current.st_size) != (row['dev'], row['ino'], row['size']):
                raise RuntimeError('SSD file changed during reset-cache; stop the writer and retry')
            row['resident_pages_before'] = _resident_pages(fd, row['size'])
            before += row['resident_pages_before']
            if execute:
                os.fsync(fd)
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                row['resident_pages_after'] = _resident_pages(fd, row['size'])
                after += row['resident_pages_after']
        finally:
            os.close(fd)
    return {'unique_regular_files': len(rows),
        'logical_bytes': sum(row['size'] for row in rows),
        'allocated_bytes': sum(row['allocated_bytes'] for row in rows),
        'requested_evict_bytes': sum(row['size'] for row in rows) if execute else 0,
        'resident_pages_before': before, 'resident_pages_after': after if execute else None,
        'observed_resident_page_reduction': before-after if execute else None,
        'files': rows}


def reset_cache(spec, *, execute: bool = False) -> dict:
    """Preview by default. Apply only advisory eviction on owned SSD inodes."""
    from . import base_cache, snapshot_host
    if os.name != 'posix' or not hasattr(os, 'posix_fadvise') or not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise RuntimeError('reset-cache requires Linux root; use bash scripts/clawbox --sudo experiment snapshot-storage reset-cache HOST')
    started = time.monotonic()
    state = json.loads(snapshot_host._state_path(spec).read_text())
    if state.get('configuration') != snapshot_host._configuration(spec):
        raise RuntimeError('reset-cache plan differs from owned host state; recover/reapply the saved storage plan')
    validation = snapshot_host.validate_peer_storage(SimpleNamespace(
        **snapshot_host.storage_configuration(spec), host_receipt=spec.receipt))
    warm = Path(spec.shared_dram.root).resolve(strict=True)
    roots = {name: Path(node.local_mount).resolve(strict=True) for name, node in spec.nodes.items()}
    if any(root == warm or root.is_relative_to(warm) or warm.is_relative_to(root) for root in roots.values()):
        raise RuntimeError('SSD roots must not overlap the shared WARM tree')
    # Existing experiment lease prevents formal VM creation during maintenance;
    # _maintenance also verifies no native VMM process is alive anywhere.
    with base_cache._maintenance():
        _assert_no_receivers()
        files = {name: _files(root) for name, root in roots.items()}
        seen = set()
        for name, rows in files.items():
            unique = []
            for row in rows:
                key = row['dev'], row['ino']
                if key not in seen:
                    unique.append(row)
                    seen.add(key)
            files[name] = unique
        backing_files = {}
        for name, node in spec.nodes.items():
            owned = state['loops'][name]
            if owned['image'] != node.image_path or owned['mount'] != node.local_mount:
                raise RuntimeError('backing image ownership differs from applied plan')
            image = Path(node.image_path)
            value = image.lstat()
            if (not stat.S_ISREG(value.st_mode) or image.resolve().is_relative_to(warm)
                    or not image.resolve().is_relative_to(Path(node.backing_mount).resolve())):
                raise RuntimeError('owned backing image must be a regular file inside its reviewed backing mount, outside WARM')
            key = value.st_dev, value.st_ino
            backing_files[name] = ([] if key in seen else [{'path': str(image),
                'dev': value.st_dev, 'ino': value.st_ino, 'size': value.st_size,
                'allocated_bytes': value.st_blocks*512}])
            seen.add(key)
        report = {'operation': 'peer_ssd_page_cache_reset', 'applied': False,
            'host_receipt': validation['receipt'], 'warm_preserved': str(warm),
            'scope': 'regular files in the two owned mounted SSD domains and their exact owned loop images; unique device/inode, no WARM or global drop_caches',
            'eviction_method': 'fsync then POSIX_FADV_DONTNEED; advisory, not guaranteed',
            'residency_measurement': 'full mincore pages over PROT_NONE mappings; no data reads/page faults',
            'page_size': os.sysconf('SC_PAGE_SIZE'), 'nodes': {}}
        for name, rows in files.items():
            report['nodes'][name] = {'root': str(roots[name]), **_reset_files(rows, execute=execute)}
        # Inner fsync completes first; then discard outer buffered-loop pages,
        # including preparation remnants when formal loops now use direct I/O.
        report['backing_images'] = {name: {'owned_image': spec.nodes[name].image_path,
            **_reset_files(rows, execute=execute)} for name, rows in backing_files.items()}
        _assert_no_receivers()
        base_cache._assert_idle()
        report.update(applied=execute, elapsed_seconds=time.monotonic()-started)
        return report
