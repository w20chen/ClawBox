"""Lossless page-local Zstd storage for native Cube RAM snapshot layers."""
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


def _allocated(path: Path) -> int:
    stat = path.stat()
    return int(stat.st_blocks) * 512


def _atomic_json(path: Path, value: Any) -> None:
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
    sidecar = Path(str(snapshot) + ".lineage.json")
    candidates: list[Path] = [snapshot]
    if sidecar.exists():
        lineage = json.loads(sidecar.read_text(encoding="utf-8"))
        for layer in lineage.get("layers") or []:
            if layer.get("external_path") is None:
                candidates.append(Path(str(snapshot) + ".layers") / str(layer["name"]))
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
    native["compression"] = dict(document["metrics"])
    return native


def compress_snapshot(path: str | Path, *, binary: str,
                      native_snapshot_metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = Path(path)
    manifest_path = compression_manifest(snapshot)
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))["metrics"]
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
        baseline = sum(int(item["metrics"]["input_allocated_bytes"]) for item in records)
        containers = sum(_allocated(Path(item["container"])) for item in records)
        aggregate = {
            "algorithm": "zstd-page",
            "zstd_level": 1,
            "page_size": records[0]["metrics"]["page_size"],
            "group_count": len(records),
            "shared_allocated_bytes_skipped": shared_skipped,
            "baseline_allocated_bytes": baseline,
            "compressed_container_allocated_bytes": containers,
            "zero_pages": sum(int(item["metrics"]["zero_pages"]) for item in records),
            "zstd_pages": sum(int(item["metrics"]["zstd_pages"]) for item in records),
            "raw_pages": sum(int(item["metrics"]["raw_pages"]) for item in records),
            "page_count": sum(int(item["metrics"]["page_count"]) for item in records),
            "logical_bytes": sum(int(item["metrics"]["logical_bytes"]) for item in records),
            "index_bytes": sum(int(item["metrics"]["index_bytes"]) for item in records),
            "payload_bytes": sum(int(item["metrics"]["payload_bytes"]) for item in records),
            "compress_wall_seconds": sum(float(item["metrics"]["wall_seconds"]) for item in records),
            "compress_cpu_seconds": sum(float(item["metrics"]["cpu_seconds"]) for item in records),
            "compress_peak_rss_kib": max(int(item["metrics"]["peak_rss_kib"]) for item in records),
            "started_unix_s": started,
            "completed_unix_s": time.time(),
        }
        document = {"schema_version": 1, "snapshot_path": str(snapshot), "groups": records,
                    "metrics": aggregate,
                    "native_snapshot_metrics": dict(native_snapshot_metrics or {})}
        _atomic_json(manifest_path, document)
        aggregate["compression_manifest_allocated_bytes"] = _allocated(manifest_path)
        aggregate["compressed_allocated_bytes"] = containers + aggregate["compression_manifest_allocated_bytes"]
        aggregate["saved_allocated_bytes"] = baseline - aggregate["compressed_allocated_bytes"]
        aggregate["space_saving_fraction"] = (
            aggregate["saved_allocated_bytes"] / baseline if baseline else 0.0
        )
        aggregate["compress_logical_mib_per_second"] = (
            aggregate["logical_bytes"] / 1048576 / aggregate["compress_wall_seconds"]
            if aggregate["compress_wall_seconds"] else 0.0
        )
        document["metrics"] = aggregate
        _atomic_json(manifest_path, document)
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
