#!/usr/bin/env python3
"""Validate real Tool Bridge + native guest eBPF integration evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def diagnose(root: Path) -> dict[str, Any]:
    """Return compact evidence that remains useful on partial test failures."""
    report: dict[str, Any] = {"records": [], "native": {}}
    log_path = root / "tool-bridge.jsonl"
    if log_path.is_file():
        report["records"] = [
            json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    for path in sorted((root / "tool-resource").glob("clause-telemetry-*.json")):
        artifact = _load_json(path)
        calls = artifact.get("calls") or []
        call = calls[0] if calls else {}
        report["native"][path.name] = {
            "collection_validity": artifact.get("collection_validity"),
            "cleanup": artifact.get("cleanup"),
            "loss_total": int(
                (artifact.get("telemetry_loss_total") or {}).get("total") or 0
            ),
            "invalid_reasons": call.get("invalid_reasons") or [],
            "candidate_rejections": call.get("candidate_rejections") or [],
            "runtime_argv": [
                invocation.get("argv") or []
                for invocation in call.get("runtime_invocations") or []
            ],
        }
    return report


def validate(root: Path) -> dict[str, Any]:
    log_path = root / "tool-bridge.jsonl"
    records = {
        row["execution_id"]: row
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for row in [json.loads(line)]
    }
    expected = {
        "exec-long",
        "exec-pipeline",
        "exec-exit",
        "exec-timeout",
        "exec-concurrent-a",
        "exec-concurrent-b",
        "exec-helper-failopen",
    }
    missing = sorted(expected - records.keys())
    if missing:
        raise AssertionError(f"missing execution records: {missing}")
    if records["exec-exit"]["exit_code"] != 7:
        raise AssertionError("nonzero exit code was not preserved")
    if records["exec-timeout"]["exit_code"] != 124 or not records["exec-timeout"]["timed_out"]:
        raise AssertionError("timeout semantics were not preserved")
    failopen = records["exec-helper-failopen"]
    if failopen["exit_code"] != 0 or failopen["telemetry_state"] != "failed":
        raise AssertionError("collector failure did not fail open with explicit status")

    native: dict[str, dict[str, Any]] = {}
    for execution_id in expected - {"exec-helper-failopen"}:
        record = records[execution_id]
        artifact_path = Path(record.get("telemetry_artifact") or "")
        if record.get("telemetry_state") != "complete" or not artifact_path.is_file():
            raise AssertionError(f"native telemetry incomplete for {execution_id}: {record}")
        artifact = _load_json(artifact_path)
        if artifact.get("cleanup") != "ok":
            raise AssertionError(f"native cleanup failed for {execution_id}")
        if int((artifact.get("telemetry_loss_total") or {}).get("total") or 0) != 0:
            raise AssertionError(f"native telemetry loss for {execution_id}")
        calls = artifact.get("calls") or []
        if len(calls) != 1 or calls[0].get("tool_call_id") != execution_id:
            raise AssertionError(f"native call identity mismatch for {execution_id}")
        native[execution_id] = artifact
        resource = root / "tool-resource" / f"cgroup-resource-{execution_id}.json"
        if not resource.is_file():
            raise AssertionError(f"independent cgroup artifact missing for {execution_id}")

    long_call = native["exec-long"]["calls"][0]
    clauses = long_call.get("clauses") or []
    if long_call.get("eligible_for_kb") is not True:
        raise AssertionError("controlled long command is not KB eligible")
    if not any(float(row.get("peak_cpu_cores") or 0) > 0 for row in clauses):
        raise AssertionError("controlled command has no positive CPU peak")
    if not any(float(row.get("sampled_peak_rss_mb") or 0) > 0 for row in clauses):
        raise AssertionError("controlled command has no positive RSS peak")

    concurrent = [native["exec-concurrent-a"], native["exec-concurrent-b"]]
    cgroup_ids = {int(artifact.get("cgroup_id") or 0) for artifact in concurrent}
    if len(cgroup_ids) != 2 or 0 in cgroup_ids:
        raise AssertionError(f"concurrent executions do not have distinct cgroups: {cgroup_ids}")
    for execution_id, other in (
        ("exec-concurrent-a", "count=16385"),
        ("exec-concurrent-b", "count=16383"),
    ):
        runtime_argv = [
            str(arg)
            for invocation in native[execution_id]["calls"][0].get("runtime_invocations") or []
            for arg in invocation.get("argv") or []
        ]
        if other in runtime_argv:
            raise AssertionError(f"cross-attributed concurrent argv in {execution_id}")

    return {
        "ok": True,
        "schema": "clawbox.toolbridge-guest-ebpf-integration.v1",
        "execution_count": len(expected),
        "native_artifact_count": len(native),
        "concurrent_cgroup_ids": sorted(cgroup_ids),
        "long_peak_cpu_cores": max(float(row.get("peak_cpu_cores") or 0) for row in clauses),
        "long_peak_rss_mb": max(float(row.get("sampled_peak_rss_mb") or 0) for row in clauses),
        "helper_failure_mode": failopen["telemetry_state"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    try:
        result = validate(args.root)
    except BaseException:
        print(json.dumps(diagnose(args.root), indent=2, sort_keys=True))
        raise
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
