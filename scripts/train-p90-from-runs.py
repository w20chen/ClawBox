#!/usr/bin/env python3
"""Train a repository-level immutable P90 file from trusted prior run artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from clawbox.cell.p90 import AdmissionPrediction
from clawbox.tuning.__main__ import find_run_traces
from clawbox.tuning.dataset import build_joined_dataset, read_cgroup_artifacts
from clawbox.tuning.native import _clawtune_api


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _evidence_paths(trace_dir: Path, bridge: Path) -> list[Path]:
    paths = {bridge, *trace_dir.glob("*.jsonl"), *trace_dir.glob("cgroup-resource-*.json")}
    tool_resource = trace_dir / "tool-resource"
    if tool_resource.is_dir():
        paths.update(tool_resource.glob("*.json"))
    return sorted(paths, key=lambda path: str(path))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tenant", default="offline-research")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--training-set-id", default="training")
    parser.add_argument(
        "--observed-repo-fingerprint",
        default=None,
        help="expected repo field in the raw agent trace when it differs from --repository",
    )
    parser.add_argument("--held-out-run", type=Path, default=None)
    parser.add_argument("--held-out-trace", type=Path, default=None)
    parser.add_argument("--held-out-workload", default=None)
    parser.add_argument(
        "--evaluation-trace", action="append", default=[], metavar="WORKLOAD=PATH",
        help="independent evaluation trace; repeat once per workload",
    )
    parser.add_argument("--evaluation-set-id", default=None)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if any(value is not None for value in (
        args.held_out_run, args.held_out_trace, args.held_out_workload,
    )) and not all(value is not None for value in (
        args.held_out_run, args.held_out_trace, args.held_out_workload,
    )):
        parser.error("--held-out-run, --held-out-trace, and --held-out-workload are required together")
    resolved_runs = {run.resolve() for run in args.runs}
    if args.held_out_run is not None and args.held_out_run.resolve() in resolved_runs:
        parser.error("--held-out-run must not also appear in the training runs")
    if args.evaluation_trace and any(value is not None for value in (
        args.held_out_run, args.held_out_trace, args.held_out_workload,
    )):
        parser.error("--evaluation-trace and --held-out-* modes are mutually exclusive")
    if bool(args.evaluation_trace) != bool(args.evaluation_set_id):
        parser.error("--evaluation-trace and --evaluation-set-id are required together")
    _, CompletedCall, RuntimeToolResourceKB, ToolCallQuery, _, _ = _clawtune_api()
    calls = []
    source = hashlib.sha256()
    run_reports: list[dict[str, Any]] = []
    expected_observed_repo = args.observed_repo_fingerprint or args.repository
    for run_index, run in enumerate(sorted(args.runs, key=str)):
        trace_dir, bridge = find_run_traces(run)
        evidence_paths = _evidence_paths(trace_dir, bridge)
        for path in evidence_paths:
            try:
                relative = path.relative_to(run)
            except ValueError:
                relative = Path(path.name)
            source.update(
                f"run-{run_index}/{relative.as_posix()}".encode()
                + b"\0" + path.read_bytes() + b"\0"
            )
        joined, trusted = build_joined_dataset(trace_dir, bridge)
        observed_repos = sorted({item.repo_fingerprint for item in trusted if item.repo_fingerprint})
        if observed_repos != [expected_observed_repo]:
            raise ValueError(
                f"{run}: observed trace repositories {observed_repos!r}; "
                f"expected exactly {expected_observed_repo!r}"
            )
        eligible_count = 0
        for item in trusted:
            if (item.start_time is None or item.end_time is None
                    or item.cpu_utilization_avg_cores is None or item.rss_peak_bytes is None):
                continue
            eligible_count += 1
            calls.append(CompletedCall(
                repo=args.repository, tool_name=item.tool_name or "exec", command=item.command,
                ts_start=item.start_time.timestamp(), ts_end=item.end_time.timestamp(),
                censored=item.exit_code not in (None, 0),
                peak_cpu_cores=float(item.cpu_utilization_avg_cores),
                peak_cpu_cores_eligible=True,
                peak_memory_mb=float(item.rss_peak_bytes) / (1024.0 * 1024.0),
                peak_memory_mb_eligible=True, ambient_before_mb=0.0,
            ))
        cgroup_files = [path for path in evidence_paths if path.suffix == ".json"]
        cgroup_loaded = read_cgroup_artifacts(trace_dir)
        run_reports.append({
            "run_id": f"{args.training_set_id}/{run.name}",
            "source_path": str(run.resolve()),
            "trace_files": len(list(trace_dir.glob("*.jsonl"))),
            "bridge_records": len({item.execution_id for item in joined.unmatched_bridges})
                              + len({item.execution_id for item in joined.joined}),
            "cgroup_files": len(cgroup_files),
            "cgroup_valid": len(cgroup_loaded),
            "cgroup_invalid_or_incomplete": len(cgroup_files) - len(cgroup_loaded),
            "spans": joined.span_count,
            "joined": len(joined.joined),
            "join_rate": joined.join_rate,
            "trusted": len(trusted),
            "trainer_eligible": eligible_count,
            "observed_repo_fingerprints": observed_repos,
        })
    if len(calls) < 5:
        raise ValueError(f"only {len(calls)} trusted completed calls; need at least 5")
    kb = RuntimeToolResourceKB.fit_public(calls)
    for call in calls:
        kb.observe_completed_call(call)
    runtime = kb.to_json_obj()
    predictions = kb.query(ToolCallQuery(
        repo=args.repository, tool_name="exec", command=None,
        # Query immediately after the newest training completion.  Using wall
        # clock time makes an otherwise frozen artifact needlessly dependent
        # on when the export command happens to run.
        ts_start=max(call.ts_end for call in calls) + 1e-6,
        ambient_before_mb=0.0,
    ))
    latency, cpu, memory = (predictions[name] for name in
                            ("latency_ms", "peak_cpu_cores", "peak_memory_mb"))
    revision = subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parents[2] / "ClawTune"),
         "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    source_digest = source.hexdigest()
    pair_digest = hashlib.sha256(
        source_digest.encode() + json.dumps(runtime, sort_keys=True).encode()
    ).hexdigest()
    training = {
        "run_count": len(args.runs),
        "completed_calls": len(calls),
        "target_repository": args.repository,
        "recording_set_id": args.training_set_id,
        "expected_observed_repo_fingerprint": expected_observed_repo,
        "runs": run_reports,
    }
    evaluation = None
    if args.held_out_run is not None:
        assert args.held_out_trace is not None and args.held_out_workload is not None
        if not args.held_out_run.is_dir():
            raise FileNotFoundError(args.held_out_run)
        if not args.held_out_trace.is_file():
            raise FileNotFoundError(args.held_out_trace)
        evaluation = {
            "protocol": "leave-one-recording-out",
            "held_out_workload": args.held_out_workload,
            "held_out_run_id": args.held_out_run.name,
            "held_out_run_path": str(args.held_out_run.resolve()),
            "held_out_trace_sha256": _sha256(args.held_out_trace),
        }
    elif args.evaluation_trace:
        traces: dict[str, dict[str, str]] = {}
        for configured in args.evaluation_trace:
            workload, separator, raw_path = configured.partition("=")
            if not separator or not workload or not raw_path:
                parser.error("--evaluation-trace must use WORKLOAD=PATH")
            if workload in traces:
                parser.error(f"duplicate evaluation workload {workload!r}")
            path = Path(raw_path)
            if not path.is_file():
                raise FileNotFoundError(path)
            traces[workload] = {"path": str(path.resolve()), "sha256": _sha256(path)}
        evaluation = {
            "protocol": "independent-recording-set",
            "recording_set_id": args.evaluation_set_id,
            "traces": traces,
        }
    payload = {
        "tenant_id": args.tenant, "repo_fingerprint": args.repository,
        "generation": args.generation, "pair_digest": pair_digest,
        "source_digest": source_digest, "artifact_count": len(calls),
        "clawtune_revision": revision,
        "prediction": {
            "latency_p90_sec": float(latency.conditional_p90) / 1000.0,
            "cpu_p90_cores": float(cpu.conditional_p90),
            "memory_p90_bytes": float(memory.conditional_p90) * 1024.0 * 1024.0,
            "evidence_count": min(latency.evidence_count, cpu.evidence_count,
                                  memory.evidence_count),
            "scopes": {"latency": latency.scope, "cpu": cpu.scope,
                       "memory": memory.scope},
            "fallback_paths": {"latency": list(latency.fallback_path),
                               "cpu": list(cpu.fallback_path),
                               "memory": list(memory.fallback_path)},
        },
        "training": training,
    }
    if evaluation is not None:
        payload["evaluation"] = evaluation
    prediction = AdmissionPrediction.from_payload(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".next")
    payload.update(prediction.as_payload())
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
