#!/usr/bin/env python3
"""Paired baseline/Zstd measurement on an isolated copy of a real snapshot."""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


_MODULE_PATH = Path(__file__).parents[1] / "clawbox" / "cube" / "snapshot_compression.py"
_SPEC = importlib.util.spec_from_file_location("clawbox_snapshot_compression", _MODULE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"cannot load {_MODULE_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
compress_snapshot = _MODULE.compress_snapshot
decompress_snapshot = _MODULE.decompress_snapshot


def allocated(path: Path) -> int:
    return path.stat().st_blocks * 512


def node_shmem_bytes(node: int) -> int | None:
    path = Path(f"/sys/devices/system/node/node{node}/meminfo")
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if " Shmem:" in line:
            return int(line.split()[-2]) * 1024
    return None


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def candidates(snapshot: Path) -> list[Path]:
    result = [snapshot]
    sidecar = Path(str(snapshot) + ".lineage.json")
    if sidecar.exists():
        lineage = json.loads(sidecar.read_text())
        result.extend(
            Path(str(snapshot) + ".layers") / item["name"]
            for item in lineage.get("layers", [])
            if item.get("external_path") is None
        )
    return [path for path in result if path.exists()]


def clone_snapshot(source: Path, destination: Path) -> list[tuple[Path, Path]]:
    destination.parent.mkdir(parents=True, exist_ok=False)
    sidecar = Path(str(source) + ".lineage.json")
    if sidecar.exists():
        shutil.copy2(sidecar, Path(str(destination) + ".lineage.json"))
    source_paths = candidates(source)
    pairs: list[tuple[Path, Path]] = []
    copied: dict[tuple[int, int], Path] = {}
    source_layers = Path(str(source) + ".layers")
    destination_layers = Path(str(destination) + ".layers")
    for item in source_paths:
        target = destination if item == source else destination_layers / item.relative_to(source_layers)
        target.parent.mkdir(parents=True, exist_ok=True)
        stat = item.stat()
        key = stat.st_dev, stat.st_ino
        if key in copied:
            os.link(copied[key], target)
        else:
            subprocess.run(
                ["cp", "--sparse=always", "--reflink=auto", "--preserve=mode,timestamps",
                 str(item), str(target)], check=True,
            )
            copied[key] = target
        pairs.append((item, target))
    return pairs


def unique_allocated(paths: list[Path]) -> int:
    seen: set[tuple[int, int]] = set()
    total = 0
    for path in paths:
        stat = path.stat()
        key = stat.st_dev, stat.st_ino
        if key not in seen:
            seen.add(key)
            total += stat.st_blocks * 512
    return total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--tier", choices=["warm", "cold"], required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    parser.add_argument("--binary", default="clawbox-zstd-page-snapshot")
    parser.add_argument("--warm-node", type=int, default=2)
    args = parser.parse_args()

    destination = args.work_root / args.tier / args.snapshot.parent.name / args.snapshot.name
    args.work_root.mkdir(parents=True, exist_ok=False)
    shmem_before_clone = node_shmem_bytes(args.warm_node)
    pairs = clone_snapshot(args.snapshot, destination)
    original_digests = {str(source): digest(source) for source, _ in pairs}
    baseline_allocated = unique_allocated([target for _, target in pairs])
    shmem_baseline = node_shmem_bytes(args.warm_node)
    native = {"logical_bytes": sum(path.stat().st_size for path in {p for _, p in pairs})}
    started = time.time()
    compression = compress_snapshot(
        destination, binary=args.binary, native_snapshot_metrics=native,
    )
    shmem_compressed = node_shmem_bytes(args.warm_node)
    decompression = decompress_snapshot(destination, binary=args.binary)
    shmem_restored = node_shmem_bytes(args.warm_node)
    if decompression is None:
        raise RuntimeError("decompression did not run")
    for source, restored in pairs:
        if digest(restored) != original_digests[str(source)]:
            raise RuntimeError(f"restored bytes differ: {source}")
    result = {
        "schema_version": 1,
        "source_snapshot": str(args.snapshot),
        "tier": args.tier,
        "started_unix_s": started,
        "completed_unix_s": time.time(),
        "baseline_allocated_bytes": baseline_allocated,
        "compressed_allocated_bytes": compression["compressed_allocated_bytes"],
        "saved_allocated_bytes": compression["saved_allocated_bytes"],
        "space_saving_percent": compression["space_saving_fraction"] * 100,
        "compression_ratio": (
            compression["compressed_allocated_bytes"] / baseline_allocated
            if baseline_allocated else 0.0
        ),
        "warm_net_dram_saved_bytes": (
            compression["saved_allocated_bytes"] if args.tier == "warm" else None
        ),
        "cold_disk_saved_bytes": (
            compression["saved_allocated_bytes"] if args.tier == "cold" else None
        ),
        "node_shmem_bytes": {
            "before_clone": shmem_before_clone,
            "baseline_copy": shmem_baseline,
            "compressed_raw_released": shmem_compressed,
            "restored": shmem_restored,
        },
        "compression": compression,
        "decompression": decompression,
        "content_verified": True,
    }
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    args.output_prefix.with_suffix(".json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    row = {
        "snapshot": str(args.snapshot), "tier": args.tier,
        "baseline_allocated_bytes": baseline_allocated,
        "compressed_allocated_bytes": compression["compressed_allocated_bytes"],
        "saved_allocated_bytes": compression["saved_allocated_bytes"],
        "space_saving_percent": result["space_saving_percent"],
        "compression_ratio": result["compression_ratio"],
        "hole_chunks": compression["hole_chunks"],
        "zstd_chunks": compression["zstd_chunks"],
        "compress_wall_seconds": compression["compress_wall_seconds"],
        "compress_cpu_seconds": compression["compress_cpu_seconds"],
        "compress_mib_per_second": compression["compress_logical_mib_per_second"],
        "decompress_wall_seconds": decompression["decompress_wall_seconds"],
        "decompress_cpu_seconds": decompression["decompress_cpu_seconds"],
        "decompress_mib_per_second": decompression["decompress_logical_mib_per_second"],
        "compress_peak_rss_kib": compression["compress_peak_rss_kib"],
        "decompress_peak_rss_kib": decompression["decompress_peak_rss_kib"],
        "content_verified": True,
    }
    with args.output_prefix.with_suffix(".csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    shutil.rmtree(args.work_root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
