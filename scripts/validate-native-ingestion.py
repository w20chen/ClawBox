#!/usr/bin/env python3
"""Strict API acceptance for one signed native telemetry artifact set."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _load_flush_script():
    path = Path(__file__).with_name("kb-flush.py")
    spec = importlib.util.spec_from_file_location("clawbox_kb_flush", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _request(endpoint: str, token: str, path: str, *, body=None, params=None):
    url = f"{endpoint.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--cell-id", required=True)
    parser.add_argument("--clawtune-revision", required=True)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()

    kb_flush = _load_flush_script()
    manifest = kb_flush.build_native_manifest(
        args.trace_dir,
        tenant=args.tenant,
        repo=args.repo,
        run_id=args.run_id,
        attempt_id=args.attempt_id,
        cell_id=args.cell_id,
        collector_version="guest-collector-e91e60b",
        clawtune_revision=args.clawtune_revision,
    )
    if manifest is None:
        raise SystemExit("no eligible native artifact pairs")
    signature = kb_flush.sign_native_manifest(manifest, args.secret)
    _, replay = _request(
        args.endpoint, args.token, "/v1/kb/native-batches",
        body={"manifest": manifest, "signature": signature},
    )
    if replay.get("generation") != 1 or not replay.get("duplicate"):
        raise SystemExit(f"idempotency failed: {replay}")

    status, snapshot = _request(
        args.endpoint, args.token, "/v1/kb/native-snapshot",
        params={"tenant_id": args.tenant, "repo": args.repo},
    )
    if status != 200 or snapshot.get("generation") != 1:
        raise SystemExit(f"snapshot load failed: {status} {snapshot}")
    expected_source = hashlib.sha256(
        "\n".join(sorted(item["sha256"] for item in manifest["artifacts"])).encode()
    ).hexdigest()
    if snapshot.get("source_digest") != expected_source:
        raise SystemExit("generation source digest is not reproducible")

    from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB
    ClauseResourceKB.from_json_obj(snapshot["clause_snapshot"])
    RuntimeToolResourceKB.from_json_obj(snapshot["runtime_snapshot"])

    crossed = dict(manifest)
    crossed["tenant_id"] = args.tenant + "-crossed"
    _, rejected = _request(
        args.endpoint, args.token, "/v1/kb/native-batches",
        body={"manifest": crossed, "signature": signature},
    )
    if rejected.get("accepted") or "HMAC" not in (rejected.get("rejection_reason") or ""):
        raise SystemExit(f"cross-tenant signature binding failed: {rejected}")

    result = {
        "ok": True,
        "generation": snapshot["generation"],
        "manifest_digest": replay["manifest_digest"],
        "source_digest": snapshot["source_digest"],
        "pair_digest": snapshot["pair_digest"],
        "artifact_count": snapshot["artifact_count"],
        "evidence": snapshot["evidence"],
        "idempotent_replay": True,
        "cross_tenant_rejected": True,
        "native_roundtrip": True,
    }
    if args.rollback:
        _, rolled = _request(
            args.endpoint, args.token, "/v1/kb/native-rollback",
            body={"tenant_id": args.tenant, "repo_fingerprint": args.repo},
        )
        if rolled.get("generation") != 0:
            raise SystemExit(f"atomic rollback failed: {rolled}")
        result["rollback_generation"] = 0
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
