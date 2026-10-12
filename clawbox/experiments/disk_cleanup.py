"""Preview or remove old crash dumps; never prune experiment or VM storage."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import struct
import time


DEFAULT_ROOTS = (Path('/var/crash'), Path('/var/lib/systemd/coredump'))


def clean_dumps(roots=DEFAULT_ROOTS, *, apply=False, older_than_hours=24,
                min_size_mib=100) -> dict:
    if older_than_hours < 1 or min_size_mib < 1:
        raise ValueError('age must be >= 1 hour and minimum size >= 1 MiB')
    cutoff = time.time() - older_than_hours * 3600
    files, errors, seen = [], [], set()
    for root in roots:
        root = Path(root).absolute()
        if root.resolve() != root or root == Path(root.anchor):
            raise ValueError(f'use a dedicated crash directory, not a symlink or filesystem root: {root}')
        if not root.exists():
            continue
        device = root.stat().st_dev
        def onerror(exc):
            errors.append(str(exc))
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
            dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()
                       and (Path(directory) / name).stat().st_dev == device]
            for name in names:
                # Linux kdump and systemd-coredump names only. A large file alone
                # is never sufficient evidence that it is disposable.
                if name not in ('vmcore',) and not name.startswith('vmcore.') and not (
                    root == Path('/var/lib/systemd/coredump') and name.startswith('core.')
                ):
                    continue
                path = Path(directory) / name
                try:
                    before = path.lstat()
                    if (not stat.S_ISREG(before.st_mode) or before.st_dev != device
                            or before.st_nlink != 1 or before.st_mtime > cutoff
                            or before.st_size < min_size_mib * 1024**2 or path in seen):
                        continue
                    seen.add(path)
                    with path.open('rb') as stream:
                        header = stream.read(20)
                    # Require an actual dump header, not just a matching name.
                    elf_core = (header[:4] == b'\x7fELF' and len(header) >= 18
                                and header[5] in (1, 2)
                                and struct.unpack('<H' if header[5] == 1 else '>H', header[16:18])[0] == 4)
                    compressed_core = (root == Path('/var/lib/systemd/coredump')
                                       and (header.startswith(b'\x28\xb5\x2f\xfd')
                                            or header.startswith(b'\xfd7zXZ\x00')
                                            or header.startswith(b'\x04\x22\x4d\x18')))
                    if not (header.startswith(b'KDUMP   ') or elf_core or compressed_core):
                        continue
                    row = dict(path=str(path), bytes=before.st_size,
                               allocated_bytes=getattr(before, 'st_blocks', before.st_size // 512) * 512, deleted=False)
                    if apply:
                        now = path.lstat()
                        if (now.st_ino, now.st_size, now.st_mtime_ns) != (
                            before.st_ino, before.st_size, before.st_mtime_ns):
                            raise ValueError(f'dump changed while scanning: {path}')
                        path.unlink()
                        row['deleted'] = True
                    files.append(row)
                except (OSError, ValueError) as exc:
                    errors.append(f'{path}: {exc}')
    return dict(mode='apply' if apply else 'preview', files=files, errors=errors,
                candidate_allocated_bytes=sum(f['allocated_bytes'] for f in files),
                deleted_allocated_bytes=sum(f['allocated_bytes'] for f in files if f['deleted']))
