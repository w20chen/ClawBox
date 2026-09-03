#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time

from cubesandbox import Template

from clawbox.cube.api_retry import read_with_backoff


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
    parser.add_argument("--exposed-port", type=int, action="append")
    # Both ClawBox Cube images start envd on 49983. They do not start the
    # code-interpreter/Jupyter service, so 49999 is not the probe endpoint.
    parser.add_argument("--probe-port", type=int, default=49983)
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--expected-kernel-version",
                        default="sha256-f84e3fa28ae6")
    args = parser.parse_args()

    templates = read_with_backoff(Template.list, label="Template.list before build")
    for item in templates:
        if args.alias and item.name == args.alias and ready(item.status):
            raise RuntimeError(
                f"READY template alias already exists: {args.alias}; "
                "refusing reuse (use a unique alias)"
            )

    # Build is intentionally submitted exactly once. A 502 here is ambiguous;
    # reconcile by alias/inventory manually instead of creating a duplicate.
    try:
        build = Template.build(
            name=args.alias, image=args.image, nodes=[args.node],
            cpu_count=args.cpu_millicores,
            memory_mb=args.memory_mib, writable_layer_size="20G",
            exposed_ports=args.exposed_port or [49983], probe_port=args.probe_port,
        )
    except Exception as exc:
        inventory = read_with_backoff(Template.list, label="Template.list after ambiguous build")
        matches = [item.template_id for item in inventory if args.alias and item.name == args.alias]
        raise RuntimeError(
            f"Template.build failed ambiguously; do not resubmit. alias={args.alias!r} "
            f"inventory_matches={matches} error={exc}"
        ) from exc
    if not build.template_id or not build.build_id:
        raise RuntimeError(f"CubeAPI returned an incomplete template build: {build}")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        status = read_with_backoff(
            lambda: Template.get_build_status(build.template_id, build.build_id),
            label=f"build status {build.build_id}",
        )
        if ready(status.status):
            info = read_with_backoff(
                lambda: Template.get(build.template_id),
                label=f"template {build.template_id}",
            )
            replicas = info.replicas or []
            bound_hashes = {str(replica.get("kernel_version", "")) for replica in replicas}
            if bound_hashes != {args.expected_kernel_version}:
                raise RuntimeError(
                    f"template {info.template_id} kernel binding mismatch: {bound_hashes}; "
                    f"expected {args.expected_kernel_version}"
                )
            print(json.dumps({"template_id": info.template_id, "alias": info.name,
                              "build_id": build.build_id,
                              "image": info.image_info or args.image, "reused": False,
                              "node": args.node, "guest_kernel_component": args.expected_kernel_version,
                              "template_kernel_hashes": sorted(bound_hashes)}, sort_keys=True))
            return 0
        if status.status.lower() in {"failed", "error", "cancelled"}:
            raise RuntimeError(status.error_message or status.message or repr(status))
        time.sleep(2)
    raise TimeoutError(f"template {build.template_id} did not become READY")


if __name__ == "__main__":
    raise SystemExit(main())
