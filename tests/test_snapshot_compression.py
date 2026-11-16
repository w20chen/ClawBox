import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from clawbox.cube.snapshot_compression import (
    apply_compression_accounting,
    compress_snapshot,
    decompress_snapshot,
    is_compressed,
    read_compressed_snapshot_metadata,
    remove_compressed_snapshot,
)
from clawbox.cube import snapshot_compression as compression_module
from clawbox.cube.snapshot import read_snapshot_metadata


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


def test_compression_accounting_replaces_only_raw_owned_layers() -> None:
    native = {
        "allocated_bytes": 100_000,
        "private_delta_bytes": 90_000,
    }
    compression = {
        "baseline_allocated_bytes": 80_000,
        "compressed_allocated_bytes": 30_000,
    }

    adjusted = apply_compression_accounting(native, compression)

    assert adjusted["raw_allocated_bytes"] == 100_000
    assert adjusted["allocated_bytes"] == 50_000
    assert adjusted["raw_private_delta_bytes"] == 90_000
    assert adjusted["private_delta_bytes"] == 40_000
    assert native["allocated_bytes"] == 100_000


def test_legacy_compression_manifest_derives_adjusted_accounting(tmp_path: Path) -> None:
    snapshot = tmp_path / "vm.mem"
    manifest = Path(str(snapshot) + ".zstd-pages.json")
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "snapshot_path": str(snapshot),
        "groups": [],
        "metrics": {
            "baseline_allocated_bytes": 80_000,
            "compressed_allocated_bytes": 30_000,
        },
        "native_snapshot_metrics": {
            "allocated_bytes": 100_000,
            "private_delta_bytes": 90_000,
        },
    }))

    metadata = read_compressed_snapshot_metadata(snapshot)

    assert metadata is not None
    assert metadata["raw_allocated_bytes"] == 100_000
    assert metadata["allocated_bytes"] == 50_000
    assert metadata["raw_private_delta_bytes"] == 90_000
    assert metadata["private_delta_bytes"] == 40_000


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
        native_snapshot_metrics={
            "logical_bytes": snapshot.stat().st_size,
            "allocated_bytes": 100_000,
            "private_delta_bytes": 90_000,
        },
    )

    assert is_compressed(snapshot)
    assert not snapshot.exists() and not private.exists()
    assert compressed["chunk_size"] == 1024 * 1024
    assert compressed["hole_chunks"] == 0
    assert compressed["zstd_chunks"] == 1
    assert "raw_pages" not in compressed
    assert external.stat().st_ino == external_inode
    assert _digest(external) == external_digest
    compressed_metadata = read_compressed_snapshot_metadata(snapshot)
    assert compressed_metadata is not None
    assert compressed_metadata["raw_allocated_bytes"] == compressed["raw_native_allocated_bytes"]
    assert compressed_metadata["allocated_bytes"] == (
        compressed["raw_native_allocated_bytes"]
        - compressed["baseline_allocated_bytes"]
        + compressed["compressed_allocated_bytes"]
    )
    assert compressed_metadata["private_delta_bytes"] == (
        90_000
        - compressed["baseline_allocated_bytes"]
        + compressed["compressed_allocated_bytes"]
    )

    restored = decompress_snapshot(snapshot, binary=str(binary))

    assert restored is not None
    assert not is_compressed(snapshot)
    assert os.path.samefile(snapshot, private)
    assert _digest(snapshot) == original_digest
    assert external.stat().st_ino == external_inode
    assert _digest(external) == external_digest


def test_cold_successor_reuses_compressed_parent_layers(tmp_path: Path):
    binary = _build_helper(tmp_path)
    first = tmp_path / "cold" / "runtime-g1.mem"
    first.parent.mkdir()
    first_layers = Path(str(first) + ".layers")
    first_layers.mkdir()
    first_delta = first_layers / "000001.mem"
    first_delta.write_bytes(b"A" * (2 * 1024 * 1024))
    os.link(first_delta, first)
    Path(str(first) + ".lineage.json").write_text(json.dumps({
        "schema_version": 2,
        "layers": [{"name": "000001.mem", "external_path": None}],
    }))
    compress_snapshot(
        first, binary=str(binary),
        native_snapshot_metrics={"lineage_snapshot_id": "run/runtime/g1"},
    )

    restored = decompress_snapshot(
        first, binary=str(binary), retain_for_reuse=True,
    )

    assert restored is not None
    reuse_manifest = Path(restored["reuse_manifest_path"])
    assert reuse_manifest.is_file()
    assert not is_compressed(first)
    assert first_delta.read_bytes() == b"A" * (2 * 1024 * 1024)

    second = tmp_path / "cold" / "runtime-g2.mem"
    second_layers = Path(str(second) + ".layers")
    second_layers.mkdir()
    inherited = second_layers / "000001.mem"
    inherited.write_bytes(first_delta.read_bytes())  # native may publish a new inode
    second_delta = second_layers / "000002.mem"
    second_delta.write_bytes(b"B" * 4096)
    os.link(second_delta, second)
    Path(str(second) + ".lineage.json").write_text(json.dumps({
        "schema_version": 2,
        "layers": [
            {"name": "000001.mem", "external_path": None},
            {"name": "000002.mem", "external_path": None},
        ],
    }))

    compressed = compress_snapshot(
        second, binary=str(binary), reusable_manifest=reuse_manifest,
        native_snapshot_metrics={
            "lineage_snapshot_id": "run/runtime/g2",
            "lineage_parent_snapshot_id": "run/runtime/g1",
        },
    )

    assert compressed["reused_group_count"] == 1
    assert compressed["fresh_group_count"] == 1
    assert compressed["reuse_manifest_consumed"] is True
    assert not reuse_manifest.exists()
    assert not first.exists() and not first_delta.exists()
    manifest = json.loads(
        Path(str(second) + ".zstd-pages.json").read_text()
    )
    by_layer = {record["layer_name"]: record for record in manifest["groups"]}
    assert by_layer["000001.mem"]["reused_compressed_parent"] is True
    assert by_layer["000001.mem"]["metrics"]["codec_seconds"] == 0
    assert by_layer["000002.mem"]["reused_compressed_parent"] is False

    assert decompress_snapshot(second, binary=str(binary)) is not None
    assert inherited.read_bytes() == b"A" * (2 * 1024 * 1024)
    assert second_delta.read_bytes() == b"B" * 4096
    assert os.path.samefile(second, second_delta)


def test_cold_successor_does_not_reuse_a_different_parent(tmp_path: Path):
    binary = _build_helper(tmp_path)
    first = tmp_path / "cold" / "runtime-g1.mem"
    first.parent.mkdir()
    first_layers = Path(str(first) + ".layers")
    first_layers.mkdir()
    first_delta = first_layers / "000001.mem"
    first_delta.write_bytes(b"A" * (2 * 1024 * 1024))
    os.link(first_delta, first)
    compress_snapshot(
        first, binary=str(binary),
        native_snapshot_metrics={"lineage_snapshot_id": "run/runtime/g1"},
    )
    restored = decompress_snapshot(
        first, binary=str(binary), retain_for_reuse=True,
    )
    assert restored is not None
    reuse_manifest = Path(restored["reuse_manifest_path"])

    second = tmp_path / "cold" / "runtime-g2.mem"
    second_layers = Path(str(second) + ".layers")
    second_layers.mkdir()
    second_delta = second_layers / "000001.mem"
    second_delta.write_bytes(first_delta.read_bytes())
    os.link(second_delta, second)

    compressed = compress_snapshot(
        second, binary=str(binary), reusable_manifest=reuse_manifest,
        native_snapshot_metrics={
            "lineage_snapshot_id": "run/runtime/g2",
            "lineage_parent_snapshot_id": "run/runtime/not-g1",
        },
    )

    assert compressed["reused_group_count"] == 0
    assert compressed["fresh_group_count"] == 1
    assert compressed["reuse_manifest_consumed"] is True
    manifest = json.loads(
        Path(str(second) + ".zstd-pages.json").read_text()
    )
    assert manifest["groups"][0]["reused_compressed_parent"] is False
    assert manifest["groups"][0]["metrics"]["codec_seconds"] > 0
    assert not reuse_manifest.exists()
    assert not first.exists() and not first_delta.exists()

    assert decompress_snapshot(second, binary=str(binary)) is not None
    assert second_delta.read_bytes() == b"A" * (2 * 1024 * 1024)
    assert os.path.samefile(second, second_delta)


def test_clone_container_copies_on_cross_device_link(
    tmp_path: Path, monkeypatch,
):
    source = tmp_path / "parent.zpg"
    destination = tmp_path / "successor.zpg"
    source.write_bytes(os.urandom(8192))
    source.chmod(0o640)

    def cross_device_link(_source, _destination):
        raise OSError(compression_module.errno.EXDEV, "cross-device link")

    monkeypatch.setattr(compression_module.os, "link", cross_device_link)

    method, wall_seconds, cpu_seconds = compression_module._clone_container(
        source, destination,
    )

    assert method == "copy"
    assert destination.read_bytes() == source.read_bytes()
    assert destination.stat().st_mode & 0o777 == source.stat().st_mode & 0o777
    assert wall_seconds >= 0
    assert cpu_seconds >= 0


def test_remove_retained_compression_reuse_state(tmp_path: Path):
    binary = _build_helper(tmp_path)
    snapshot = tmp_path / "cold" / "runtime-g1.mem"
    snapshot.parent.mkdir()
    layers = Path(str(snapshot) + ".layers")
    layers.mkdir()
    delta = layers / "000001.mem"
    delta.write_bytes(b"A" * (2 * 1024 * 1024))
    os.link(delta, snapshot)
    compress_snapshot(
        snapshot, binary=str(binary),
        native_snapshot_metrics={"lineage_snapshot_id": "run/runtime/g1"},
    )
    restored = decompress_snapshot(
        snapshot, binary=str(binary), retain_for_reuse=True,
    )
    assert restored is not None
    reuse_manifest = Path(restored["reuse_manifest_path"])
    document = json.loads(reuse_manifest.read_text())
    containers = [Path(record["container"]) for record in document["groups"]]
    container_directory = Path(document["container_directory"])

    remove_compressed_snapshot(snapshot)

    assert not snapshot.exists()
    assert not delta.exists()
    assert not reuse_manifest.exists()
    assert not container_directory.exists()
    assert not any(container.exists() for container in containers)


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


def test_sparse_write_coalescing_preserves_holes_and_partial_final_page(tmp_path: Path):
    binary = _build_helper(tmp_path)
    page = os.sysconf("SC_PAGESIZE")
    # Nonzero runs separated by zero pages, crossing the 1 MiB frame boundary.
    expected = (b"A" * (1024 * 1024 - page) + b"\0" * (2 * page)
                + b"B" * (3 * page) + b"\0" * page + b"tail")
    snapshot = tmp_path / "runs.mem"
    snapshot.write_bytes(expected)
    compress_snapshot(snapshot, binary=str(binary))
    restored = decompress_snapshot(snapshot, binary=str(binary))
    assert snapshot.read_bytes() == expected
    assert snapshot.stat().st_blocks * 512 < len(expected)
    assert restored is not None
    assert all(value >= 0 for value in restored["phase_seconds"].values())


def test_allocated_zeros_and_holes_roundtrip_without_losing_zero_updates(tmp_path: Path):
    binary = _build_helper(tmp_path)
    snapshot = tmp_path / "delta.mem"
    chunk = 1024 * 1024
    with snapshot.open("wb") as stream:
        stream.write(b"\0" * chunk)
        stream.seek(2 * chunk)
        stream.write(b"changed tail")
    # The new layer owns the zero range even though its older base is nonzero.
    base = tmp_path / "base.mem"
    base.write_bytes(b"A" * snapshot.stat().st_size)
    lineage = Path(str(snapshot) + ".lineage.json")
    lineage.write_text(json.dumps({"layers": [
        {"name": "base", "external_path": str(base),
         "ranges": [{"offset": chunk, "length": chunk}]},
        {"name": "delta", "ranges": [
            {"offset": 0, "length": chunk},
            {"offset": 2 * chunk, "length": len(b"changed tail")},
        ]},
    ]}))
    original_lineage = lineage.read_bytes()
    original_digest = _digest(snapshot)

    compressed = compress_snapshot(snapshot, binary=str(binary))

    assert compressed["chunk_count"] == 3
    assert compressed["zstd_chunks"] == 2  # allocated zeros and the partial tail
    assert compressed["hole_chunks"] == 1  # only the actual file hole
    assert decompress_snapshot(snapshot, binary=str(binary)) is not None
    assert _digest(snapshot) == original_digest
    assert lineage.read_bytes() == original_lineage
    assert base.read_bytes() == b"A" * snapshot.stat().st_size
    # Apply the recorded layer ownership: zero updates must override old A bytes.
    restored = bytearray(base.read_bytes())
    delta = snapshot.read_bytes()
    for owned in json.loads(lineage.read_text())["layers"][1]["ranges"]:
        start, length = owned["offset"], owned["length"]
        restored[start:start + length] = delta[start:start + length]
    assert restored == b"\0" * chunk + b"A" * chunk + b"changed tail"


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


def test_managed_external_dependency_is_not_compressed_or_replaced(tmp_path: Path):
    binary = _build_helper(tmp_path)
    snapshot = tmp_path / "vm.mem"
    layers = Path(str(snapshot) + ".layers")
    layers.mkdir()
    base = layers / "000000.mem"
    base.write_bytes(b"A" * 8192)
    external = tmp_path / "cold" / "layers" / "cold-layer" / "000000.mem"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"C" * 8192)
    external_inode = external.stat().st_ino
    external_digest = _digest(external)
    delta = layers / "000002.mem"
    delta.write_bytes(b"B" * 8192)
    os.link(delta, snapshot)
    Path(str(snapshot) + ".lineage.json").write_text(json.dumps({
        "schema_version": 2,
        "logical_bytes": 8192,
        "transferred_bytes": 0,
        "dirty_bytes": 0,
        "full_base": False,
        "layers": [
            {"name": "000000.mem", "external_path": None},
            {
                "name": "000001.mem",
                "external_path": str(external),
                "layer_id": "cold-layer",
                "managed": True,
            },
            {"name": "000002.mem", "external_path": None},
        ],
    }))

    native_metrics = read_snapshot_metadata(str(snapshot), require_lineage=True)
    compressed = compress_snapshot(
        snapshot,
        binary=str(binary),
        native_snapshot_metrics=native_metrics,
    )

    assert external.stat().st_ino == external_inode
    assert _digest(external) == external_digest
    assert not base.exists() and not delta.exists() and not snapshot.exists()
    metadata = read_compressed_snapshot_metadata(snapshot)
    assert metadata is not None
    assert compressed["raw_native_allocated_bytes"] == native_metrics["allocated_bytes"]
    assert metadata["raw_allocated_bytes"] == compressed["raw_native_allocated_bytes"]
    assert metadata["managed_external_bytes"] == native_metrics["managed_external_bytes"]
    assert metadata["managed_external_bytes"] == external.stat().st_blocks * 512
    assert metadata["external_layer_paths"] == [str(external)]

    decompress_snapshot(snapshot, binary=str(binary))

    assert base.read_bytes() == b"A" * 8192
    assert delta.read_bytes() == b"B" * 8192
    assert os.path.samefile(snapshot, delta)
    assert external.stat().st_ino == external_inode
    assert _digest(external) == external_digest


def test_compression_does_not_require_reading_root_owned_lineage(tmp_path: Path):
    binary = _build_helper(tmp_path)
    snapshot = tmp_path / "vm.mem"
    layers = Path(str(snapshot) + ".layers")
    layers.mkdir()
    delta = layers / "000000.mem"
    delta.write_bytes(b"A" * 8192)
    os.link(delta, snapshot)
    sidecar = Path(str(snapshot) + ".lineage.json")
    sidecar.write_text(json.dumps({
        "schema_version": 2,
        "layers": [{"name": "000000.mem", "external_path": None}],
    }))
    raw_allocated = delta.stat().st_blocks * 512 + sidecar.stat().st_blocks * 512
    sidecar.chmod(0)
    try:
        compressed = compress_snapshot(
            snapshot,
            binary=str(binary),
            native_snapshot_metrics={"allocated_bytes": 8192},
        )
        manifest = Path(str(snapshot) + ".zstd-pages.json")
        assert compressed["raw_native_allocated_bytes"] == raw_allocated
        assert compressed["compression_manifest_allocated_bytes"] == (
            manifest.stat().st_blocks * 512
        )
        assert compressed["compressed_allocated_bytes"] == (
            compressed["compressed_container_allocated_bytes"]
            + compressed["compression_manifest_allocated_bytes"]
        )
        metadata = read_compressed_snapshot_metadata(snapshot)
        assert metadata is not None
        assert metadata["raw_allocated_bytes"] == raw_allocated
        assert metadata["allocated_bytes"] == (
            raw_allocated
            - compressed["baseline_allocated_bytes"]
            + compressed["compressed_allocated_bytes"]
        )
    finally:
        sidecar.chmod(0o600)
    assert not delta.exists() and not snapshot.exists()

    decompress_snapshot(snapshot, binary=str(binary))

    assert delta.read_bytes() == b"A" * 8192
    assert os.path.samefile(snapshot, delta)


def test_manifest_accounting_uses_actual_allocation_after_rewrite(
    tmp_path: Path, monkeypatch,
):
    binary = _build_helper(tmp_path)
    snapshot = tmp_path / "vm.mem"
    snapshot.write_bytes(b"A" * 8192)
    manifest = Path(str(snapshot) + ".zstd-pages.json")
    original_atomic_json = compression_module._atomic_json
    calls = 0

    def growing_atomic_json(path: Path, value) -> None:
        nonlocal calls
        original_atomic_json(path, value)
        calls += 1
        # Simulate delayed allocation/self-referential size growth that used to
        # exhaust the convergence loop.
        with path.open("a", encoding="utf-8") as output:
            output.write(" " * 4096)
            output.flush()
            os.fsync(output.fileno())

    monkeypatch.setattr(compression_module, "_atomic_json", growing_atomic_json)

    compressed = compress_snapshot(snapshot, binary=str(binary))

    assert calls == 2
    assert compressed["compression_manifest_allocated_bytes"] == (
        manifest.stat().st_blocks * 512
    )
    metadata = compression_module.read_compressed_snapshot_metadata(snapshot)
    assert metadata is not None
    assert metadata["compression"]["compression_manifest_allocated_bytes"] == (
        manifest.stat().st_blocks * 512
    )
    assert metadata["compression"]["compressed_allocated_bytes"] == (
        metadata["compression"]["compressed_container_allocated_bytes"]
        + manifest.stat().st_blocks * 512
    )
    with manifest.open("a", encoding="utf-8") as output:
        output.write(" " * 4096)
        output.flush()
        os.fsync(output.fileno())
    retried = compress_snapshot(snapshot, binary=str(binary))
    assert retried["compression_manifest_allocated_bytes"] == (
        manifest.stat().st_blocks * 512
    )
