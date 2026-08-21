#!/usr/bin/env python3
"""Safely remove Kubernetes pods whose API phase is already Failed."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict


def kubectl(*args: str, capture: bool = False, quiet: bool = False) -> str:
    result = subprocess.run(
        ["kubectl", *args],
        check=True,
        text=True,
        stdout=(subprocess.PIPE if capture else subprocess.DEVNULL if quiet else None),
    )
    return result.stdout or ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="delete the enumerated Failed pods; without this flag, only report",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--namespace", action="append", dest="namespaces",
        help="limit reporting/deletion to this namespace; may be repeated",
    )
    scope.add_argument(
        "--all-namespaces", action="store_true",
        help="explicitly allow deletion across every namespace",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 500:
        parser.error("--batch-size must be between 1 and 500")
    if args.apply and not (args.namespaces or args.all_namespaces):
        parser.error("--apply requires --namespace NAME or --all-namespaces")

    payload = json.loads(kubectl("get", "pods", "-A", "-o", "json", capture=True))
    by_namespace: dict[str, list[str]] = defaultdict(list)
    for item in payload.get("items", []):
        if item.get("status", {}).get("phase") != "Failed":
            continue
        metadata = item["metadata"]
        if args.namespaces and metadata["namespace"] not in args.namespaces:
            continue
        by_namespace[metadata["namespace"]].append(metadata["name"])

    total = sum(len(names) for names in by_namespace.values())
    for namespace, names in sorted(by_namespace.items()):
        print(f"{namespace}: {len(names)} Failed pods")
    print(f"total: {total} Failed pods")
    if not args.apply or total == 0:
        return 0

    for namespace, names in sorted(by_namespace.items()):
        for offset in range(0, len(names), args.batch_size):
            batch = names[offset:offset + args.batch_size]
            kubectl(
                "-n", namespace, "delete", "pod", *batch,
                "--ignore-not-found=true", "--wait=false",
                quiet=True,
            )
            print(f"deleted {len(batch)} from {namespace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
