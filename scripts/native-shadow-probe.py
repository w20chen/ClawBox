#!/usr/bin/env python3
"""Record one pre-execution native prediction for controlled shadow acceptance."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--trace-output", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--ts-start", type=float, default=None)
    args = parser.parse_args()

    from clawtune_sidecar.predictors.tool_resource import _tool_resource_prediction_payload
    from tool_resource.runtime_kb import (
        ClauseResourceKB,
        LatencyBuckets,
        RuntimeToolResourceKB,
        ToolCallQuery,
    )

    metadata = json.loads(
        (args.artifact_dir / "native-kb-load.json").read_text(encoding="utf-8")
    )
    if metadata["repo_fingerprint"] != args.repo:
        raise ValueError("probe repository does not match loaded snapshot")
    clause = ClauseResourceKB.from_json_obj(json.loads(
        (args.artifact_dir / "clause-resource-kb.json").read_text(encoding="utf-8")
    ))
    runtime = RuntimeToolResourceKB.from_json_obj(json.loads(
        (args.artifact_dir / "runtime-tool-resource-kb.json").read_text(encoding="utf-8")
    ))
    ts_start = args.ts_start if args.ts_start is not None else time.time()
    clause_prediction = clause.predict_command_latency_bucket(
        args.repo,
        args.command,
        ts_start,
        LatencyBuckets((100.0, 500.0, 2_000.0, 10_000.0)),
    )
    continuous = runtime.query(ToolCallQuery(
        repo=args.repo,
        tool_name="exec",
        command=args.command,
        ts_start=ts_start,
        ambient_before_mb=0.0,
    ))
    payload = _tool_resource_prediction_payload(
        clause_prediction,
        continuous_predictions={key: asdict(value) for key, value in continuous.items()},
    )
    record = {
        "schema_version": 6,
        "record_type": "span_start",
        "run_id": args.run_id,
        "kind": "tool",
        "name": "exec",
        "execution": {"execution_id": args.execution_id},
        "prediction": {"tool_resource": payload},
    }
    args.trace_output.parent.mkdir(parents=True, exist_ok=True)
    args.trace_output.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "generation": metadata["generation"],
        "pair_digest": metadata["pair_digest"],
        "evidence_runs": metadata["evidence"]["runs"],
        "prediction": payload,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
