#!/usr/bin/env python3
"""Build and inject the minimal stateful runtime agent into a Kata rootfs."""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
from pathlib import Path


def run(*argv: str) -> None:
    subprocess.run(argv, check=True)


def extract_first_mbr_partition(source: Path, output: Path) -> tuple[int, int]:
    with source.open("rb") as handle:
        mbr = handle.read(512)
        if len(mbr) != 512 or mbr[510:512] != b"\x55\xaa":
            raise ValueError(f"{source} has no valid MBR")
        entry = mbr[446:462]
        first_sector, sector_count = struct.unpack_from("<II", entry, 8)
        if first_sector <= 0 or sector_count <= 0:
            raise ValueError(f"{source} first MBR partition is empty")
        handle.seek(first_sector * 512)
        remaining = sector_count * 512
        with output.open("xb") as destination:
            while remaining:
                chunk = handle.read(min(8 * 1024 * 1024, remaining))
                if not chunk:
                    raise ValueError(f"{source} ended inside its first partition")
                destination.write(chunk)
                remaining -= len(chunk)
    return first_sector, sector_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-image", required=True, type=Path)
    parser.add_argument("--agent-source", required=True, type=Path)
    parser.add_argument("--output-rootfs", required=True, type=Path)
    parser.add_argument("--output-agent", type=Path)
    parser.add_argument("--compiler", default="gcc")
    parser.add_argument("--debugfs", default="debugfs")
    args = parser.parse_args()
    if args.output_rootfs.exists():
        raise FileExistsError(args.output_rootfs)
    output_agent = args.output_agent or args.output_rootfs.with_suffix(".agent")
    if output_agent.exists():
        raise FileExistsError(output_agent)
    output_agent.parent.mkdir(parents=True, exist_ok=True)
    args.output_rootfs.parent.mkdir(parents=True, exist_ok=True)

    try:
        run(args.compiler, "-static", "-O2", "-Wall", "-Wextra", "-Werror",
            "-o", str(output_agent), str(args.agent_source))
        extract_first_mbr_partition(args.base_image, args.output_rootfs)
        run(args.debugfs, "-w", "-R", f"write {output_agent} /clawbox-runtime-agent",
            str(args.output_rootfs))
        run(args.debugfs, "-w", "-R", "set_inode_field /clawbox-runtime-agent mode 0100755",
            str(args.output_rootfs))
        run(args.debugfs, "-R", "stat /clawbox-runtime-agent", str(args.output_rootfs))
        shutil.copymode(args.base_image, args.output_rootfs)
    except Exception:
        args.output_rootfs.unlink(missing_ok=True)
        output_agent.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
