#!/usr/bin/env python3
"""Render deploy manifests with immutable ClawBox platform image digests."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
DEFAULT_OUTPUT = ROOT / ".artifacts" / "rendered-deploy"
FILES = (
    "cell-controller.yaml",
    "trace-ingester.yaml",
    "tune-kb.yaml",
    "managed-control-plane.yaml",
)
IMAGE_RE = re.compile(r"^[a-zA-Z0-9._:/-]+@sha256:[0-9a-f]{64}$")
REPLACEMENTS = {
    re.compile(r"127\.0\.0\.1:5000/clawbox/control-plane-arm64:(?:dev|fixed2)"): "control",
    re.compile(r"127\.0\.0\.1:5000/clawbox/runtime-arm64:dev"): "runtime",
    re.compile(r"127\.0\.0\.1:5000/clawbox/tool-bridge-arm64:dev"): "tool_bridge",
}


def immutable_image(value: str) -> str:
    if not IMAGE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("image must use IMAGE@sha256:<64 lowercase hex> form")
    return value


def render(output: Path, images: dict[str, str]) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    rendered: list[Path] = []
    for name in FILES:
        text = (DEPLOY / name).read_text(encoding="utf-8")
        for pattern, key in REPLACEMENTS.items():
            text = pattern.sub(images[key], text)
        leftovers = [pattern.pattern for pattern in REPLACEMENTS if pattern.search(text)]
        if leftovers:
            raise RuntimeError(f"mutable ClawBox image remains in {name}: {leftovers}")
        destination = output / name
        destination.write_text(text, encoding="utf-8", newline="\n")
        rendered.append(destination)
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-image", required=True, type=immutable_image)
    parser.add_argument("--runtime-image", required=True, type=immutable_image)
    parser.add_argument("--tool-bridge-image", required=True, type=immutable_image)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    paths = render(
        args.output_dir,
        {
            "control": args.control_image,
            "runtime": args.runtime_image,
            "tool_bridge": args.tool_bridge_image,
        },
    )
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
