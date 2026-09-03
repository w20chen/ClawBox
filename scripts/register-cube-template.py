#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time

from cubesandbox import Template


def ready(value: str) -> bool:
    return value.lower() in {"ready", "succeeded", "success", "completed"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Register one ARM64 CubeSandbox template")
    parser.add_argument("image", help="prefer an immutable image digest")
    parser.add_argument("--alias")
    parser.add_argument("--node", required=True)
    parser.add_argument(
        "--cpu-millicores", type=int, default=2000,
        help="template CPU quota in millicores (default: 2000 = 2 vCPU)",
    )
    parser.add_argument("--memory-mib", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=1200)
    args = parser.parse_args()

    for item in Template.list():
        if args.alias and item.name == args.alias and ready(item.status):
            print(json.dumps({"template_id": item.template_id, "alias": item.name,
                              "image": item.image_info, "reused": True}, sort_keys=True))
            return 0

    build = Template.build(
        name=args.alias, image=args.image, nodes=[args.node],
        cpu_count=args.cpu_millicores,
        memory_mb=args.memory_mib, writable_layer_size="20G",
    )
    if not build.template_id or not build.build_id:
        raise RuntimeError(f"CubeAPI returned an incomplete template build: {build}")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        status = Template.get_build_status(build.template_id, build.build_id)
        if ready(status.status):
            info = Template.get(build.template_id)
            print(json.dumps({"template_id": info.template_id, "alias": info.name,
                              "image": info.image_info or args.image, "reused": False}, sort_keys=True))
            return 0
        if status.status.lower() in {"failed", "error", "cancelled"}:
            raise RuntimeError(status.error_message or status.message or repr(status))
        time.sleep(2)
    raise TimeoutError(f"template {build.template_id} did not become READY")


if __name__ == "__main__":
    raise SystemExit(main())
