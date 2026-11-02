#!/usr/bin/env python3
"""Aggregate JSON records emitted by benchmark-zstd-snapshot.py."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)
    args = parser.parse_args()
    records = [json.loads(path.read_text()) for path in args.inputs]
    baseline = sum(item["baseline_allocated_bytes"] for item in records)
    compressed = sum(item["compressed_allocated_bytes"] for item in records)
    compression_wall = sum(item["compression"]["compress_wall_seconds"] for item in records)
    decompression_wall = sum(item["decompression"]["decompress_wall_seconds"] for item in records)
    logical = sum(item["compression"]["logical_bytes"] for item in records)
    pages = sum(item["compression"]["page_count"] for item in records)
    summary = {
        "schema_version": 1,
        "snapshot_count": len(records),
        "baseline_allocated_bytes": baseline,
        "compressed_allocated_bytes": compressed,
        "saved_allocated_bytes": baseline - compressed,
        "space_saving_percent": (baseline - compressed) / baseline * 100 if baseline else 0,
        "compression_ratio": compressed / baseline if baseline else 0,
        "warm_net_dram_saved_bytes": sum(item["warm_net_dram_saved_bytes"] or 0 for item in records),
        "cold_disk_saved_bytes": sum(item["cold_disk_saved_bytes"] or 0 for item in records),
        "logical_bytes": logical,
        "page_count": pages,
        "zero_pages": sum(item["compression"]["zero_pages"] for item in records),
        "zstd_pages": sum(item["compression"]["zstd_pages"] for item in records),
        "raw_pages": sum(item["compression"]["raw_pages"] for item in records),
        "compress_wall_seconds": compression_wall,
        "compress_cpu_seconds": sum(item["compression"]["compress_cpu_seconds"] for item in records),
        "compress_mib_per_second": logical / 1048576 / compression_wall if compression_wall else 0,
        "decompress_wall_seconds": decompression_wall,
        "decompress_cpu_seconds": sum(item["decompression"]["decompress_cpu_seconds"] for item in records),
        "decompress_mib_per_second": logical / 1048576 / decompression_wall if decompression_wall else 0,
        "compress_peak_rss_kib": max(item["compression"]["compress_peak_rss_kib"] for item in records),
        "decompress_peak_rss_kib": max(item["decompression"]["decompress_peak_rss_kib"] for item in records),
        "all_content_verified": all(item["content_verified"] for item in records),
        "snapshots": records,
    }
    for encoding in ("zero", "zstd", "raw"):
        summary[f"{encoding}_page_percent"] = (
            summary[f"{encoding}_pages"] / pages * 100 if pages else 0
        )
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    flat = {key: value for key, value in summary.items() if key != "snapshots"}
    with args.csv.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flat))
        writer.writeheader()
        writer.writerow(flat)
    print(json.dumps(flat, sort_keys=True))


if __name__ == "__main__":
    main()
