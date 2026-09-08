#!/usr/bin/env python3
"""Prepare BCC headers with the configuration of the kernel a Cube VM actually boots."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import subprocess

from cubesandbox import NEVER_TIMEOUT, Sandbox


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, help="Existing Tool template")
    parser.add_argument("--node", required=True)
    parser.add_argument("--image", required=True, help="Tool image containing the matching kernel source")
    parser.add_argument("--tag", required=True, help="New Docker image tag")
    parser.add_argument("--output", required=True, type=Path, help="New build evidence directory")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    sandbox = Sandbox.create(
        template=args.template, timeout=NEVER_TIMEOUT,
        lifecycle={"on_timeout": "kill", "auto_resume": False},
        distribution_scope=[args.node],
        metadata={"clawbox.owner": "kernel-header-preparation"},
    )
    try:
        result = sandbox.commands.run(
            "uname -r; zcat /proc/config.gz", timeout=30,
        )
        if result.exit_code:
            raise RuntimeError(result.stderr)
        release, config = result.stdout.split("\n", 1)
        if not config.startswith("#") or "CONFIG_ARM64=y" not in config:
            raise RuntimeError("Guest did not return its ARM64 kernel configuration")
        current = sandbox.commands.run(
            "cat /lib/modules/$(uname -r)/build/.config", timeout=30,
        )
        (root / "previous-header.config").write_text(current.stdout)
    finally:
        sandbox.kill()
    (root / "running-kernel.config").write_text(config)
    source = shlex.quote("/lib/modules/" + release + "/build")
    (root / "Dockerfile").write_text(
        "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n"
        "COPY running-kernel.config /tmp/clawbox-running-kernel.config\n"
        f"RUN test -d {source} && cp /tmp/clawbox-running-kernel.config {source}/.config "
        f"&& make -C {source} olddefconfig prepare modules_prepare\n"
    )
    with (root / "build.log").open("w") as log:
        subprocess.run(
            ["docker", "build", "--build-arg", "BASE_IMAGE=" + args.image,
             "-t", args.tag, str(root)], stdout=log, stderr=subprocess.STDOUT, check=True,
        )
    (root / "result.json").write_text(json.dumps({
        "kernel_release": release, "source_template": args.template,
        "base_image": args.image, "image": args.tag,
    }, indent=2) + "\n")
    print(f"Built {args.tag} for guest kernel {release}. Push it and register a new Tool template.")


if __name__ == "__main__":
    main()
