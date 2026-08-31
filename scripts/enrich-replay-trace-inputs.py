#!/usr/bin/env python3
"""Freeze observed gateway requests into an output-only replay trace."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    records = [
        json.loads(line) for line in args.trace.read_text().splitlines()
        if line.strip()
    ]
    llm_records = [
        record for record in records
        if record.get("type") == "action" and record.get("action_type") == "llm_call"
    ]
    requests = json.loads(args.requests.read_text())
    payloads = [item.get("request_payload") for item in requests]
    if len(llm_records) != len(payloads):
        raise ValueError(
            f"LLM/request count mismatch: {len(llm_records)} != {len(payloads)}"
        )
    if any(
        not isinstance(payload, dict) or not isinstance(payload.get("messages"), list)
        for payload in payloads
    ):
        raise ValueError("every observed request must contain a messages array")

    for record, payload in zip(llm_records, payloads, strict=True):
        data = record.setdefault("data", {})
        if data.get("raw_request") or data.get("messages_in"):
            raise ValueError("input trace is already enriched")
        data["raw_request"] = payload

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".next")
    temporary.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(json.dumps({
        "schema_version": 1,
        "steps": len(llm_records),
        "source_trace": str(args.trace),
        "source_trace_sha256": sha256(args.trace),
        "source_requests": str(args.requests),
        "source_requests_sha256": sha256(args.requests),
        "output": str(args.output),
        "output_sha256": sha256(args.output),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
