#!/usr/bin/env python3
"""Extract deduplicated page-compression metrics from a ClawBox event JSONL."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterator


def walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def mean(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return sum(values) / len(values) if values else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("events", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--session-progress", type=Path)
    args = parser.parse_args()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    parse_errors = 0
    with args.events.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            for record in walk(event):
                operation = record.get("operation")
                if operation not in {"checkpoint", "spill", "restore"}:
                    continue
                if "started_unix_s" not in record or "service_seconds" not in record:
                    continue
                metrics = record.get("snapshot_metrics") or {}
                compression = metrics.get("compression")
                decompression = metrics.get("decompression")
                if not isinstance(compression, dict) and not isinstance(decompression, dict):
                    continue
                key = (
                    operation, record.get("role"), record.get("started_unix_s"),
                    record.get("snapshot_generation"), record.get("snapshot_path"),
                )
                if key in seen:
                    continue
                seen.add(key)
                stage = record.get("stage_seconds") or {}
                row: dict[str, Any] = {
                    "operation": operation,
                    "role": record.get("role"),
                    "started_unix_s": record.get("started_unix_s"),
                    "completed_unix_s": record.get("completed_unix_s"),
                    "snapshot_generation": record.get("snapshot_generation"),
                    "snapshot_path": record.get("snapshot_path"),
                    "tier_from": record.get("tier_from"),
                    "tier_to": record.get("tier_to"),
                    "total_service_seconds": record.get("service_seconds"),
                    "native_api_checkpoint_seconds": stage.get("api_checkpoint_s"),
                    "native_api_restore_seconds": stage.get("api_restore_s"),
                    "compression_stage_seconds": stage.get("snapshot_compress_s"),
                    "decompression_stage_seconds": stage.get("snapshot_decompress_s"),
                }
                if isinstance(compression, dict):
                    row.update({
                        "codec_action": "compress",
                        **{key: compression.get(key) for key in (
                            "algorithm", "zstd_level", "page_size", "logical_bytes",
                            "page_count", "zero_pages", "zstd_pages", "raw_pages",
                            "index_bytes", "payload_bytes", "baseline_allocated_bytes",
                            "compressed_allocated_bytes", "saved_allocated_bytes",
                            "space_saving_fraction", "compress_wall_seconds",
                            "compress_cpu_seconds", "compress_logical_mib_per_second",
                            "compress_peak_rss_kib", "shared_allocated_bytes_skipped",
                        )},
                    })
                else:
                    original = decompression.get("compression_metrics") or {}
                    row.update({
                        "codec_action": "decompress",
                        "algorithm": decompression.get("algorithm"),
                        "zstd_level": decompression.get("zstd_level"),
                        "logical_bytes": original.get("logical_bytes"),
                        "page_count": original.get("page_count"),
                        "zero_pages": original.get("zero_pages"),
                        "zstd_pages": original.get("zstd_pages"),
                        "raw_pages": original.get("raw_pages"),
                        "baseline_allocated_bytes": original.get("baseline_allocated_bytes"),
                        "compressed_allocated_bytes": original.get("compressed_allocated_bytes"),
                        "saved_allocated_bytes": original.get("saved_allocated_bytes"),
                        "space_saving_fraction": original.get("space_saving_fraction"),
                        "decompress_wall_seconds": decompression.get("decompress_wall_seconds"),
                        "decompress_cpu_seconds": decompression.get("decompress_cpu_seconds"),
                        "decompress_logical_mib_per_second": decompression.get(
                            "decompress_logical_mib_per_second"
                        ),
                        "decompress_peak_rss_kib": decompression.get("decompress_peak_rss_kib"),
                        "restored_allocated_bytes": decompression.get("restored_allocated_bytes"),
                    })
                rows.append(row)
    rows.sort(key=lambda item: (float(item["started_unix_s"]), str(item["role"])))
    compressed = [row for row in rows if row["codec_action"] == "compress"]
    decompressed = [row for row in rows if row["codec_action"] == "decompress"]
    total_pages = sum(int(row.get("page_count") or 0) for row in compressed)
    compressed_logical = sum(int(row.get("logical_bytes") or 0) for row in compressed)
    decompressed_logical = sum(int(row.get("logical_bytes") or 0) for row in decompressed)
    baseline = sum(int(row.get("baseline_allocated_bytes") or 0) for row in compressed)
    stored = sum(int(row.get("compressed_allocated_bytes") or 0) for row in compressed)
    compression_wall = sum(float(row.get("compress_wall_seconds") or 0) for row in compressed)
    decompression_wall = sum(float(row.get("decompress_wall_seconds") or 0) for row in decompressed)
    summary = {
        "schema_version": 1,
        "source_events": str(args.events),
        "parse_errors": parse_errors,
        "compression_count": len(compressed),
        "decompression_count": len(decompressed),
        "warm_compression_count": sum(row.get("tier_to") == "warm" for row in compressed),
        "cold_compression_count": sum(row.get("tier_to") == "cold" for row in compressed),
        "baseline_allocated_bytes": baseline,
        "compressed_allocated_bytes": stored,
        "saved_allocated_bytes": baseline - stored,
        "space_saving_percent": (baseline - stored) / baseline * 100 if baseline else 0,
        "page_count": total_pages,
        "zero_pages": sum(int(row.get("zero_pages") or 0) for row in compressed),
        "zstd_pages": sum(int(row.get("zstd_pages") or 0) for row in compressed),
        "raw_pages": sum(int(row.get("raw_pages") or 0) for row in compressed),
        "compression_logical_bytes": compressed_logical,
        "compression_wall_seconds": compression_wall,
        "compression_cpu_seconds": sum(float(row.get("compress_cpu_seconds") or 0) for row in compressed),
        "compression_mib_per_second": (
            compressed_logical / 1048576 / compression_wall if compression_wall else 0
        ),
        "decompression_logical_bytes": decompressed_logical,
        "decompression_wall_seconds": decompression_wall,
        "decompression_cpu_seconds": sum(float(row.get("decompress_cpu_seconds") or 0) for row in decompressed),
        "decompression_mib_per_second": (
            decompressed_logical / 1048576 / decompression_wall if decompression_wall else 0
        ),
        "checkpoint_total_seconds_mean": mean(
            [row for row in compressed if row["operation"] == "checkpoint"],
            "total_service_seconds",
        ),
        "checkpoint_native_api_seconds_mean": mean(
            [row for row in compressed if row["operation"] == "checkpoint"],
            "native_api_checkpoint_seconds",
        ),
        "checkpoint_compression_added_seconds_mean": mean(
            [row for row in compressed if row["operation"] == "checkpoint"],
            "compression_stage_seconds",
        ),
        "restore_total_seconds_mean": mean(decompressed, "total_service_seconds"),
        "restore_native_api_seconds_mean": mean(decompressed, "native_api_restore_seconds"),
        "restore_decompression_added_seconds_mean": mean(
            decompressed, "decompression_stage_seconds"
        ),
        "compress_peak_rss_kib": max(
            (int(row.get("compress_peak_rss_kib") or 0) for row in compressed), default=0
        ),
        "decompress_peak_rss_kib": max(
            (int(row.get("decompress_peak_rss_kib") or 0) for row in decompressed), default=0
        ),
        "peak_rss_note": (
            "Historical Full events used getrusage ru_maxrss, which retains the worker high-water "
            "mark across exec; use the isolated benchmark peak RSS for codec memory. New helpers "
            "read exec-local /proc/self/status VmHWM."
        ),
    }
    if args.session_progress:
        statuses: dict[str, int] = {}
        for path in args.session_progress.glob("*.json"):
            status = str(json.loads(path.read_text()).get("status") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
        summary["session_status_counts"] = statuses
    summary["tiers"] = {}
    for tier in ("warm", "cold"):
        tier_rows = [row for row in compressed if row.get("tier_to") == tier]
        tier_baseline = sum(int(row.get("baseline_allocated_bytes") or 0) for row in tier_rows)
        tier_stored = sum(int(row.get("compressed_allocated_bytes") or 0) for row in tier_rows)
        summary["tiers"][tier] = {
            "compression_count": len(tier_rows),
            "baseline_allocated_bytes": tier_baseline,
            "compressed_allocated_bytes": tier_stored,
            "saved_allocated_bytes": tier_baseline - tier_stored,
            "space_saving_percent": (
                (tier_baseline - tier_stored) / tier_baseline * 100 if tier_baseline else 0
            ),
            "compress_wall_seconds_mean": mean(tier_rows, "compress_wall_seconds"),
            "compress_cpu_seconds_mean": mean(tier_rows, "compress_cpu_seconds"),
        }
    for encoding in ("zero", "zstd", "raw"):
        summary[f"{encoding}_page_percent"] = (
            summary[f"{encoding}_pages"] / total_pages * 100 if total_pages else 0
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "snapshots.json").write_text(
        json.dumps(rows, indent=2, sort_keys=True) + "\n"
    )
    fields = sorted({key for row in rows for key in row})
    with (args.output_dir / "snapshots.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    flat_summary = {
        key: value for key, value in summary.items() if key not in {"tiers", "session_status_counts"}
    }
    flat_summary["session_status_counts_json"] = json.dumps(
        summary.get("session_status_counts", {}), sort_keys=True
    )
    flat_summary["tiers_json"] = json.dumps(summary["tiers"], sort_keys=True)
    with (args.output_dir / "summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flat_summary))
        writer.writeheader()
        writer.writerow(flat_summary)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
