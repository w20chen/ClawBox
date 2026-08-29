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
    parser.add_argument(
        "--extra-space-mib", type=int, default=512,
        help="extend the extracted ext4 filesystem before agent/workspace injection",
    )
    parser.add_argument(
        "--workspace-source", type=Path,
        help="optional disposable workspace copied into /workspace in the image",
    )
    parser.add_argument("--compiler", default="gcc")
    parser.add_argument("--debugfs", default="debugfs")
    args = parser.parse_args()
    if args.extra_space_mib < 1:
        parser.error("--extra-space-mib must be positive")
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
        extend_ext4(args.output_rootfs, args.extra_space_mib)
        run(args.debugfs, "-w", "-R", f"write {output_agent} /clawbox-runtime-agent",
            str(args.output_rootfs))
        run(args.debugfs, "-w", "-R", "set_inode_field /clawbox-runtime-agent mode 0100755",
            str(args.output_rootfs))
        if args.workspace_source is not None:
            copy_workspace(args.debugfs, args.workspace_source, args.output_rootfs)
        run(args.debugfs, "-R", "stat /clawbox-runtime-agent", str(args.output_rootfs))
        shutil.copymode(args.base_image, args.output_rootfs)
    except Exception:
        args.output_rootfs.unlink(missing_ok=True)
        output_agent.unlink(missing_ok=True)
        raise


def extend_ext4(rootfs: Path, extra_space_mib: int) -> None:
    """Create real free ext4 blocks; Kata's extracted root partition is full."""
    with rootfs.open("ab") as handle:
        handle.truncate(rootfs.stat().st_size + extra_space_mib * 1024 * 1024)
    run("resize2fs", str(rootfs))


def copy_workspace(debugfs: str, source: Path, rootfs: Path) -> None:
    """Copy a disposable regular-file workspace into the Tool image.

    The experiment deliberately rejects special files and symlinks rather than
    silently changing their meaning inside the VM.  A benchmark workspace is
    expected to be a checked-out repository, so regular files and directories
    are sufficient and keep this setup path auditable.
    """
    source = source.resolve()
    if not source.is_dir():
        raise ValueError(f"workspace source is not a directory: {source}")
    if any(char.isspace() or char in {'"', "'"} for char in str(source)):
        raise ValueError("workspace source path must not contain whitespace or quotes")
    run(debugfs, "-w", "-R", "mkdir /workspace", str(rootfs))
    ignored_parts = {".git", ".venv", ".artifacts", ".pytest_cache", "__pycache__"}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if ignored_parts.intersection(path.relative_to(source).parts):
            continue
        destination = f"/workspace/{relative}"
        if any(char.isspace() or char in {'"', "'"} for char in relative):
            raise ValueError(f"workspace path is not debugfs-safe: {relative!r}")
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f"workspace contains unsupported file type: {path}")
        if path.is_dir():
            run(debugfs, "-w", "-R", f"mkdir {destination}", str(rootfs))
        else:
            run(debugfs, "-w", "-R", f"write {path} {destination}", str(rootfs))


if __name__ == "__main__":
    main()
