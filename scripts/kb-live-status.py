#!/usr/bin/env python3
"""Query tune-KB generation from inside its authenticated API container."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:8086")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()

    token = os.environ["CLAWBOX_SERVICE_TOKEN"]
    query = urllib.parse.urlencode({"tenant_id": args.tenant, "repo": args.repo})
    request = urllib.request.Request(
        f"{args.endpoint.rstrip('/')}/v1/kb/generation?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        print(json.dumps(json.load(response), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
