#!/usr/bin/env python3
"""Export an ARM64 OCI image into a bootable ext4 Firecracker rootfs."""
from __future__ import annotations
import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(*argv: str, capture: bool = False) -> str:
    result = subprocess.run(argv, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def injection(value: str) -> tuple[Path, str]:
    if value.count(":") != 1:
        raise ValueError("--inject-file must be SOURCE:ABSOLUTE_DESTINATION")
    source_text, destination = value.split(":", 1)
    source = Path(source_text).resolve()
    if not source.is_file() or not destination.startswith("/") or ".." in Path(destination).parts:
        raise ValueError(f"invalid injection: {value}")
    return source, destination


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--image", required=True)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--size-mib", required=True, type=int)
    p.add_argument("--inject-file", action="append", default=[])
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(a.output)
    if a.size_mib < 512:
        raise ValueError("rootfs size must be at least 512 MiB")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    container = ""
    try:
        with tempfile.TemporaryDirectory(prefix="clawbox-oci-rootfs-") as temporary:
            root = Path(temporary) / "root"
            archive = Path(temporary) / "image.tar"
            root.mkdir()
            container = run("docker", "create", a.image, capture=True)
            run("docker", "export", "-o", str(archive), container)
            run("tar", "--numeric-owner", "--xattrs", "-xf", str(archive), "-C", str(root))
            for item in a.inject_file:
                source, destination = injection(item)
                target = root / destination.lstrip("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            for directory in ("dev", "proc", "sys", "run", "tmp", "state", "workspace"):
                (root / directory).mkdir(exist_ok=True)
            used_mib = int(run("du", "-sm", str(root), capture=True).split()[0])
            if used_mib + 256 > a.size_mib:
                raise ValueError(f"--size-mib {a.size_mib} is too small; image uses {used_mib} MiB")
            run("truncate", "-s", f"{a.size_mib}M", str(a.output))
            run("mkfs.ext4", "-q", "-F", "-d", str(root), str(a.output))
    except Exception:
        a.output.unlink(missing_ok=True)
        raise
    finally:
        if container:
            subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL)


if __name__ == "__main__": main()
