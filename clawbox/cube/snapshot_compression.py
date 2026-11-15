"""Lossless sparse-chunk Zstd storage for native Cube RAM snapshot layers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any


SUFFIX = ".zstd-pages.json"
DIRECTORY_SUFFIX = ".zstd-pages"


def _allocated(path: Path, stat: os.stat_result | None = None) -> int:
    stat = stat or path.stat()
    return int(stat.st_blocks) * 512


def _atomic_json(path: Path, value: Any) -> None:
    temporary = Path(str(path) + ".tmp")
    data = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as output:
        output.write(data)
        output.flush()
        # The manifest accounts for its own physical allocation. Delayed
        # allocation can otherwise make st_blocks change while this function is
        # still iterating, producing a false non-convergence failure.
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _run(binary: str, operation: str, source: Path, destination: Path) -> dict[str, Any]:
    executable = shutil.which(binary) if not Path(binary).is_absolute() else binary
    if not executable or not Path(executable).is_file():
        raise RuntimeError(
            f"snapshot compression binary not found: {binary}; run scripts/build-zstd-page-snapshot.sh"
        )
    completed = subprocess.run(
        [str(executable), operation, str(source), str(destination)],
        text=True, capture_output=True, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"snapshot {operation} failed ({completed.returncode}): {completed.stderr.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"snapshot {operation} returned invalid metrics") from exc


def _owned_groups(snapshot: Path) -> tuple[list[list[Path]], int]:
    candidates: list[Path] = [snapshot]
    layers = Path(str(snapshot) + ".layers")
    if layers.is_dir():
        candidates.extend(sorted(layers.glob("*.mem")))
    groups: dict[tuple[int, int], list[Path]] = {}
    for path in candidates:
        if not path.exists():
            continue
        stat = path.stat()
        groups.setdefault((stat.st_dev, stat.st_ino), []).append(path)
    safe: list[list[Path]] = []
    skipped = 0
    for paths in groups.values():
        unique = list(dict.fromkeys(paths))
        # Never replace an inode referenced outside this snapshot layout: that
        # would copy data which Cube intentionally shares through CoW/hardlinks.
        if unique[0].stat().st_nlink > len(unique):
            skipped += _allocated(unique[0])
            continue
        safe.append(unique)
    return safe, skipped


def apply_compression_accounting(
    native_snapshot_metrics: dict[str, Any] | None,
    compression: dict[str, Any],
) -> dict[str, Any]:
    """Replace raw owned-layer allocation with the compressed representation.

    The native manifest allocation also contains lineage sidecars and any
    shared/external files that compression intentionally skipped. Subtract only
    the raw owned-layer baseline and add the compressed container+manifest.
    """
    metrics = dict(native_snapshot_metrics or {})
    allocated = compression.get("raw_native_allocated_bytes")
    if type(allocated) is not int:
        allocated = metrics.get("allocated_bytes")
    baseline = compression.get("baseline_allocated_bytes")
    compressed = compression.get("compressed_allocated_bytes")
    if type(allocated) is not int or type(baseline) is not int or type(compressed) is not int:
        return metrics
    adjusted = max(0, allocated - baseline + compressed)
    metrics["raw_allocated_bytes"] = allocated
    metrics["allocated_bytes"] = adjusted
    private_delta = metrics.get("private_delta_bytes")
    if type(private_delta) is not int:
        private_delta = baseline
    if type(private_delta) is int:
        metrics["raw_private_delta_bytes"] = private_delta
        metrics["private_delta_bytes"] = max(0, private_delta - baseline + compressed)
    return metrics


def compression_manifest(path: str | Path) -> Path:
    return Path(str(path) + SUFFIX)


def is_compressed(path: str | Path) -> bool:
    return compression_manifest(path).is_file()


def read_compressed_snapshot_metadata(path: str | Path) -> dict[str, Any] | None:
    manifest = compression_manifest(path)
    if not manifest.exists():
        return None
    document = json.loads(manifest.read_text(encoding="utf-8"))
    native = dict(document.get("native_snapshot_metrics") or {})
    compression = dict(document["metrics"])
    if "compressed_container_allocated_bytes" in compression:
        compression = _refresh_manifest_accounting(
            compression, _allocated(manifest),
        )
    # The manifest records the representation it replaced. Older manifests may
    # already contain adjusted fields and preserve the original values in the
    # raw_* keys. Reconstruct the raw accounting before applying the current
    # manifest allocation so a restart never trusts a stale self-reference.
    if "raw_allocated_bytes" in native:
        native["allocated_bytes"] = native["raw_allocated_bytes"]
    if "raw_private_delta_bytes" in native:
        native["private_delta_bytes"] = native["raw_private_delta_bytes"]
    native = apply_compression_accounting(native, compression)
    native["compression"] = compression
    return native


def _refresh_manifest_accounting(
    metrics: dict[str, Any], manifest_bytes: int,
) -> dict[str, Any]:
    refreshed = dict(metrics)
    containers = int(
        refreshed.get("compressed_container_allocated_bytes") or 0
    )
    baseline = int(refreshed.get("baseline_allocated_bytes") or 0)
    compressed = containers + manifest_bytes
    refreshed["compression_manifest_allocated_bytes"] = manifest_bytes
    refreshed["compressed_allocated_bytes"] = compressed
    refreshed["saved_allocated_bytes"] = baseline - compressed
    refreshed["space_saving_fraction"] = (
        refreshed["saved_allocated_bytes"] / baseline if baseline else 0.0
    )
    wall_seconds = float(refreshed.get("compress_wall_seconds") or 0.0)
    logical_bytes = int(refreshed.get("logical_bytes") or 0)
    refreshed["compress_logical_mib_per_second"] = (
        logical_bytes / 1048576 / wall_seconds if wall_seconds else 0.0
    )
    return refreshed


def compress_snapshot(path: str | Path, *, binary: str,
                      native_snapshot_metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = Path(path)
    manifest_path = compression_manifest(snapshot)
    if manifest_path.exists():
        metrics = json.loads(manifest_path.read_text(encoding="utf-8"))["metrics"]
        if "compressed_container_allocated_bytes" in metrics:
            return _refresh_manifest_accounting(metrics, _allocated(manifest_path))
        return metrics
    groups, shared_skipped = _owned_groups(snapshot)
    if not groups:
        raise RuntimeError(f"snapshot has no exclusively owned RAM layer to compress: {snapshot}")
    container_dir = Path(str(snapshot) + DIRECTORY_SUFFIX)
    container_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    records: list[dict[str, Any]] = []
    started = time.time()
    try:
        for index, paths in enumerate(groups):
            source = paths[0]
            container = container_dir / f"{index:06d}.zpg"
            temporary = Path(str(container) + ".tmp")
            metrics = _run(binary, "compress", source, temporary)
            os.replace(temporary, container)
            records.append({
                "paths": [str(item) for item in paths],
                "mode": source.stat().st_mode & 0o777,
                "container": str(container),
                "metrics": metrics,
            })
        raw_unique: dict[tuple[int, int], int] = {}
        raw_candidates = [snapshot]
        layers_dir = Path(str(snapshot) + ".layers")
        if layers_dir.is_dir():
            raw_candidates.extend(sorted(layers_dir.glob("*.mem")))
        sidecar = Path(str(snapshot) + ".lineage.json")
        if sidecar.exists():
            raw_candidates.append(sidecar)
        for path in raw_candidates:
            if not path.exists():
                continue
            stat = path.stat()
            raw_unique[(stat.st_dev, stat.st_ino)] = _allocated(path, stat)
        baseline = sum(int(item["metrics"]["input_allocated_bytes"]) for item in records)
        containers = sum(_allocated(Path(item["container"])) for item in records)
        aggregate = {
            "algorithm": "zstd-page",
            "zstd_level": 1,
            "chunk_size": records[0]["metrics"]["chunk_size"],
            "group_count": len(records),
            "shared_allocated_bytes_skipped": shared_skipped,
            "raw_native_allocated_bytes": sum(raw_unique.values()),
            "baseline_allocated_bytes": baseline,
            "compressed_container_allocated_bytes": containers,
            "hole_chunks": sum(int(item["metrics"]["hole_chunks"]) for item in records),
            "zstd_chunks": sum(int(item["metrics"]["zstd_chunks"]) for item in records),
            "chunk_count": sum(int(item["metrics"]["chunk_count"]) for item in records),
            "logical_bytes": sum(int(item["metrics"]["logical_bytes"]) for item in records),
            "index_bytes": sum(int(item["metrics"]["index_bytes"]) for item in records),
            "payload_bytes": sum(int(item["metrics"]["payload_bytes"]) for item in records),
            "compress_wall_seconds": sum(float(item["metrics"]["wall_seconds"]) for item in records),
            "compress_cpu_seconds": sum(float(item["metrics"]["cpu_seconds"]) for item in records),
            "compress_peak_rss_kib": max(int(item["metrics"]["peak_rss_kib"]) for item in records),
            "phase_seconds": {
                phase: (sum(float(item["metrics"][phase + "_seconds"]) for item in records)
                        if all(phase + "_seconds" in item["metrics"] for item in records) else None)
                for phase in ("read", "codec", "write", "sync")
            },
            "started_unix_s": started,
            "completed_unix_s": time.time(),
        }
        raw_native_metrics = dict(native_snapshot_metrics or {})
        aggregate = _refresh_manifest_accounting(aggregate, 0)
        document = {
            "schema_version": 1,
            "snapshot_path": str(snapshot),
            "groups": records,
            "metrics": aggregate,
            "native_snapshot_metrics": apply_compression_accounting(
                raw_native_metrics, aggregate,
            ),
        }
        _atomic_json(manifest_path, document)
        aggregate = _refresh_manifest_accounting(
            aggregate, _allocated(manifest_path),
        )
        document = {
            "schema_version": 1,
            "snapshot_path": str(snapshot),
            "groups": records,
            "metrics": aggregate,
            "native_snapshot_metrics": apply_compression_accounting(
                raw_native_metrics, aggregate,
            ),
        }
        _atomic_json(manifest_path, document)
        # A filesystem can choose a different allocation for the final JSON
        # representation. The returned accounting must describe that actual
        # file; readers perform the same correction from the persisted stat.
        aggregate = _refresh_manifest_accounting(
            aggregate, _allocated(manifest_path),
        )
        for record in records:
            for raw in record["paths"]:
                Path(raw).unlink(missing_ok=True)
        return aggregate
    except Exception:
        for record in records:
            Path(record["container"]).unlink(missing_ok=True)
        shutil.rmtree(container_dir, ignore_errors=True)
        manifest_path.unlink(missing_ok=True)
        raise


def decompress_snapshot(path: str | Path, *, binary: str) -> dict[str, Any] | None:
    snapshot = Path(path)
    manifest_path = compression_manifest(snapshot)
    if not manifest_path.exists():
        return None
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    started = time.time()
    for record in document["groups"]:
        paths = [Path(item) for item in record["paths"]]
        canonical = paths[0]
        if not canonical.exists():
            canonical.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(str(canonical) + ".decompressing")
            temporary.unlink(missing_ok=True)
            metrics = _run(binary, "decompress", Path(record["container"]), temporary)
            os.chmod(temporary, int(record["mode"]))
            os.replace(temporary, canonical)
            results.append(metrics)
        for alias in paths[1:]:
            if not alias.exists():
                alias.parent.mkdir(parents=True, exist_ok=True)
                os.link(canonical, alias)
        if not all(path.exists() and os.path.samefile(canonical, path) for path in paths):
            raise RuntimeError(f"snapshot decompression did not restore hardlink identity: {snapshot}")
    for record in document["groups"]:
        Path(record["container"]).unlink(missing_ok=True)
    shutil.rmtree(Path(str(snapshot) + DIRECTORY_SUFFIX), ignore_errors=True)
    manifest_path.unlink(missing_ok=True)
    wall_seconds = sum(float(item["wall_seconds"]) for item in results)
    logical_bytes = int(document["metrics"].get("logical_bytes") or 0)
    return {
        "algorithm": "zstd-page",
        "zstd_level": 1,
        "decompress_wall_seconds": wall_seconds,
        "decompress_cpu_seconds": sum(float(item["cpu_seconds"]) for item in results),
        "decompress_peak_rss_kib": max((int(item["peak_rss_kib"]) for item in results), default=0),
        "phase_seconds": {
            phase: (sum(float(item[phase + "_seconds"]) for item in results)
                    if results and all(phase + "_seconds" in item for item in results) else None)
            for phase in ("read", "codec", "write", "sync")
        },
        "started_unix_s": started,
        "completed_unix_s": time.time(),
        "restored_allocated_bytes": sum(_allocated(Path(record["paths"][0])) for record in document["groups"]),
        "decompress_logical_mib_per_second": (
            logical_bytes / 1048576 / wall_seconds if wall_seconds else 0.0
        ),
        "compression_metrics": document["metrics"],
    }


def remove_compressed_snapshot(path: str | Path) -> None:
    manifest = compression_manifest(path)
    directory = Path(str(path) + DIRECTORY_SUFFIX)
    if manifest.exists():
        document = json.loads(manifest.read_text(encoding="utf-8"))
        for record in document.get("groups") or []:
            Path(record["container"]).unlink(missing_ok=True)
    shutil.rmtree(directory, ignore_errors=True)
    manifest.unlink(missing_ok=True)
