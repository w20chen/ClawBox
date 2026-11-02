import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from clawbox.cube.snapshot_compression import (
    compress_snapshot,
    decompress_snapshot,
    is_compressed,
)


def _build_helper(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "native" / "zstd_page_snapshot.c"
    binary = tmp_path / "clawbox-zstd-page-snapshot"
    try:
        completed = subprocess.run(
            ["gcc", "-O2", "-std=gnu11", str(source), "-Wl,-l:libzstd.so.1", "-o", str(binary)],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("gcc is unavailable")
    if completed.returncode:
        pytest.skip(f"libzstd build unavailable: {completed.stderr}")
    return binary


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_page_container_roundtrip_preserves_lineage_and_shared_base(tmp_path: Path):
    binary = _build_helper(tmp_path)
    snapshot = tmp_path / "vm.mem"
    layers = Path(str(snapshot) + ".layers")
    layers.mkdir()
    private = layers / "private.mem"
    private.write_bytes(b"\0" * 4096 + b"A" * 4096 + os.urandom(4096))
    os.link(private, snapshot)
    external = tmp_path / "template.mem"
    external.write_bytes(b"template-shared" * 4096)
    external_inode = external.stat().st_ino
    external_digest = _digest(external)
    Path(str(snapshot) + ".lineage.json").write_text(json.dumps({
        "layers": [
            {"name": "template", "external_path": str(external)},
            {"name": "private.mem", "external_path": None},
        ]
    }))
    original_digest = _digest(snapshot)

    compressed = compress_snapshot(
        snapshot,
        binary=str(binary),
        native_snapshot_metrics={"logical_bytes": snapshot.stat().st_size},
    )

    assert is_compressed(snapshot)
    assert not snapshot.exists() and not private.exists()
    assert compressed["chunk_size"] == 1024 * 1024
    assert compressed["hole_chunks"] == 0
    assert compressed["zstd_chunks"] == 1
    assert "raw_pages" not in compressed
    assert external.stat().st_ino == external_inode
    assert _digest(external) == external_digest

    restored = decompress_snapshot(snapshot, binary=str(binary))

    assert restored is not None
    assert not is_compressed(snapshot)
    assert os.path.samefile(snapshot, private)
    assert _digest(snapshot) == original_digest
    assert external.stat().st_ino == external_inode
    assert _digest(external) == external_digest


def test_incompressible_chunk_stays_in_zstd_format(tmp_path: Path):
    binary = _build_helper(tmp_path)
    snapshot = tmp_path / "random.mem"
    snapshot.write_bytes(os.urandom(1024 * 1024))
    original_digest = _digest(snapshot)

    compressed = compress_snapshot(snapshot, binary=str(binary))

    assert compressed["zstd_chunks"] == 1
    assert "raw_pages" not in compressed
    assert decompress_snapshot(snapshot, binary=str(binary)) is not None
    assert _digest(snapshot) == original_digest


def test_external_hardlink_prevents_unintended_copy(tmp_path: Path):
    binary = _build_helper(tmp_path)
    snapshot = tmp_path / "vm.mem"
    snapshot.write_bytes(b"A" * 8192)
    outside = tmp_path / "reader.mem"
    os.link(snapshot, outside)

    with pytest.raises(RuntimeError, match="no exclusively owned"):
        compress_snapshot(snapshot, binary=str(binary))

    assert os.path.samefile(snapshot, outside)
    assert outside.read_bytes() == b"A" * 8192
