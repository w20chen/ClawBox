#!/usr/bin/env python3
"""Fetch, natively validate, and atomically publish a ClawTune KB pair."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def publish_response(response: dict[str, Any], artifact_dir: Path) -> dict[str, Any]:
    from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB

    clause = response["clause_snapshot"]
    runtime = response["runtime_snapshot"]
    ClauseResourceKB.from_json_obj(clause)
    RuntimeToolResourceKB.from_json_obj(runtime)
    pair_digest = hashlib.sha256(
        (canonical(clause) + "\n" + canonical(runtime)).encode()
    ).hexdigest()
    if pair_digest != response["pair_digest"]:
        raise ValueError("native snapshot pair digest mismatch")
    metadata = {
        key: response[key]
        for key in (
            "tenant_id", "repo_fingerprint", "generation", "pair_digest",
            "source_digest", "artifact_count", "clawtune_revision", "evidence",
        )
    }
    artifact_dir.mkdir(parents=True, exist_ok=True)
    targets = (
        (artifact_dir / "clause-resource-kb.json", canonical(clause)),
        (artifact_dir / "runtime-tool-resource-kb.json", canonical(runtime)),
        # Metadata is the commit marker and is published last.
        (artifact_dir / "native-kb-load.json", canonical(metadata)),
    )
    temporary: list[tuple[Path, Path]] = []
    for target, content in targets:
        pending = target.with_name(f".{target.name}.tmp")
        pending.write_text(content, encoding="utf-8")
        temporary.append((pending, target))
    for pending, target in temporary:
        os.replace(pending, target)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    args = parser.parse_args()
    query = urllib.parse.urlencode({"tenant_id": args.tenant, "repo": args.repo})
    request = urllib.request.Request(
        f"{args.endpoint.rstrip('/')}/v1/kb/native-snapshot?{query}",
        headers={"Authorization": f"Bearer {args.token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read())
    metadata = publish_response(payload, args.artifact_dir)
    print(json.dumps({
        "generation": metadata["generation"],
        "pair_digest": metadata["pair_digest"],
        "evidence_runs": metadata["evidence"]["runs"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
