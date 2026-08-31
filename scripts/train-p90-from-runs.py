#!/usr/bin/env python3
"""Train a repository-level immutable P90 file from trusted prior run artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from pathlib import Path
from typing import Any

from clawbox.cell.p90 import AdmissionPrediction
from clawbox.replay.trace import load_trace
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


def _trace_tool_calls(path: Path) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    model_step = 0
    for action in load_trace(path):
        if action.kind != "llm":
            continue
        message = action.output
        if (isinstance(message, dict) and set(message) == {"content"}
                and isinstance(message["content"], dict)):
            message = message["content"]
        if not isinstance(message, dict):
            model_step += 1
            continue
        for call_index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            command = arguments.get("command") if isinstance(arguments, dict) else None
            calls.append({
                "model_step": model_step,
                "call_index": call_index,
                "tool_name": name,
                "command": command if isinstance(command, str) else None,
            })
        model_step += 1
    return calls


def _per_tool_memory_plan(
    kb: Any,
    query_type: Any,
    *,
    repository: str,
    traces: dict[str, Path],
    query_ts: float,
    idle_tool_vm_rss_mib: float,
    idle_safety_margin_fraction: float,
    command_headroom_fraction: float,
    size_classes_mib: list[int],
) -> dict[str, Any]:
    if idle_tool_vm_rss_mib <= 0:
        raise ValueError("idle Tool-VM RSS must be positive")
    if not 0 <= idle_safety_margin_fraction <= 1:
        raise ValueError("idle safety margin must be between 0 and 1")
    if not 0 <= command_headroom_fraction <= 1:
        raise ValueError("command headroom must be between 0 and 1")
    if not size_classes_mib or size_classes_mib != sorted(set(size_classes_mib)):
        raise ValueError("Tool memory size classes must be unique and increasing")
    idle_floor_mib = math.ceil(
        idle_tool_vm_rss_mib * (1 + idle_safety_margin_fraction)
    )
    workloads: dict[str, Any] = {}
    query_index = 0
    for workload, trace in sorted(traces.items()):
        reservations = []
        for call in _trace_tool_calls(trace):
            prediction = kb.query(query_type(
                repo=repository,
                tool_name=call["tool_name"] or "exec",
                command=call["command"],
                ts_start=query_ts + query_index * 1e-6,
                ambient_before_mb=0.0,
            ))["peak_memory_mb"]
            query_index += 1
            if prediction.conditional_p90 is None:
                raise ValueError("per-tool memory prediction is unavailable")
            command_p90_mib = float(prediction.conditional_p90)
            incremental_p90_kib = math.ceil(
                command_p90_mib * (1 + command_headroom_fraction) * 1024.0
            )
            reservation_kib = math.ceil(
                idle_floor_mib * 1024.0 + incremental_p90_kib
            )
            reservation_mib = reservation_kib / 1024.0
            reservations.append({
                **call,
                "command_sha256": (
                    hashlib.sha256(call["command"].encode()).hexdigest()
                    if call["command"] is not None else None
                ),
                "predicted_command_memory_p90_mib": command_p90_mib,
                "incremental_p90_kib": incremental_p90_kib,
                "incremental_p90_mib": incremental_p90_kib / 1024.0,
                "reservation_kib": reservation_kib,
                "reservation_mib": reservation_mib,
                "scope": prediction.scope,
                "key_kind": prediction.key_kind,
                "evidence_count": prediction.evidence_count,
                "fallback_path": list(prediction.fallback_path),
            })
        if not reservations:
            raise ValueError(f"evaluation trace {workload!r} has no concrete tool calls")
        required_mib = max(item["reservation_mib"] for item in reservations)
        selected_class = next(
            (value for value in size_classes_mib if value >= required_mib), None
        )
        if selected_class is None:
            raise ValueError(
                f"{workload}: {required_mib} MiB exceeds the largest Tool size class"
            )
        workloads[workload] = {
            "trace_path": str(trace.resolve()),
            "trace_sha256": _sha256(trace),
            "tool_invocations": reservations,
            "reservation_min_mib": min(item["reservation_mib"] for item in reservations),
            "reservation_max_mib": required_mib,
            "reservation_distinct_mib": sorted({
                item["reservation_mib"] for item in reservations
            }),
            "reservation_distinct_kib": sorted({
                item["reservation_kib"] for item in reservations
            }),
            "incremental_p90_distinct_kib": sorted({
                item["incremental_p90_kib"] for item in reservations
            }),
            "selected_vm_size_class_mib": selected_class,
        }
    return {
        "semantics": (
            "admission uses live resident Tool-Firecracker RSS plus per-tool "
            "incremental P90 commitments and global safety headroom; tool completion "
            "remeasures RSS and checkpointing is the only assumed reclamation"
        ),
        "idle_tool_vm_rss_mib": idle_tool_vm_rss_mib,
        "idle_safety_margin_fraction": idle_safety_margin_fraction,
        "derived_idle_floor_mib": idle_floor_mib,
        "command_headroom_fraction": command_headroom_fraction,
        "size_classes_mib": size_classes_mib,
        "workloads": workloads,
    }


def _attach_oracle_working_sets(
    plan: dict[str, Any],
    oracle_runs: dict[str, Path],
) -> dict[str, Any]:
    reports: dict[str, Any] = {}
    idle_rss_mib = float(plan["idle_tool_vm_rss_mib"])
    capacity_by_workload = {
        name: int(item["selected_vm_size_class_mib"])
        for name, item in plan["workloads"].items()
    }
    for workload, run in sorted(oracle_runs.items()):
        if workload not in plan["workloads"]:
            raise ValueError(f"oracle workload {workload!r} is absent from the prediction plan")
        trace_dir, bridge = find_run_traces(run)
        _joined, trusted = build_joined_dataset(trace_dir, bridge)
        observations = sorted(
            [item for item in trusted
             if item.command and item.rss_peak_bytes is not None and item.start_time is not None],
            key=lambda item: item.start_time,
        )
        planned = plan["workloads"][workload]["tool_invocations"]
        cursor = 0
        matched = []
        for invocation in planned:
            command = invocation.get("command")
            match = None
            for observation_index in range(cursor, len(observations)):
                candidate = observations[observation_index]
                if candidate.command == command:
                    match = candidate
                    cursor = observation_index + 1
                    break
            if match is None:
                continue
            actual_command_mib = float(match.rss_peak_bytes) / (1024.0 * 1024.0)
            predicted_command_mib = float(invocation["predicted_command_memory_p90_mib"])
            actual_working_set_mib = idle_rss_mib + actual_command_mib
            row = {
                "model_step": invocation["model_step"],
                "call_index": invocation["call_index"],
                "command_sha256": invocation["command_sha256"],
                "predicted_command_memory_p90_mib": predicted_command_mib,
                "actual_command_peak_memory_mib": actual_command_mib,
                "prediction_error_mib": predicted_command_mib - actual_command_mib,
                "prediction_covered_actual": predicted_command_mib >= actual_command_mib,
                "actual_working_set_mib": actual_working_set_mib,
                "oracle_reservation_mib": actual_working_set_mib,
            }
            invocation["oracle"] = row
            matched.append(row)
        if not matched:
            raise ValueError(f"oracle run {workload!r} did not match any planned tool call")
        capacity = capacity_by_workload[workload]
        max_working_set = max(item["actual_working_set_mib"] for item in matched)
        reports[workload] = {
            "run_path": str(run.resolve()),
            "matched_invocations": len(matched),
            "planned_invocations": len(planned),
            "coverage_fraction": sum(item["prediction_covered_actual"] for item in matched) / len(matched),
            "mean_prediction_error_mib": sum(item["prediction_error_mib"] for item in matched) / len(matched),
            "max_actual_command_peak_memory_mib": max(item["actual_command_peak_memory_mib"] for item in matched),
            "max_actual_working_set_mib": max_working_set,
            "selected_vm_capacity_mib": capacity,
            "vm_capacity_sufficient": max_working_set < capacity,
        }
    return {
        "semantics": "held-out actual command cgroup peak plus measured idle Tool-VM RSS; never used for admission",
        "workloads": reports,
    }


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
    parser.add_argument("--idle-tool-vm-rss-mib", type=float)
    parser.add_argument("--idle-safety-margin-fraction", type=float, default=0.25)
    parser.add_argument("--command-headroom-fraction", type=float, default=0.25)
    parser.add_argument("--tool-memory-size-class-mib", type=int, action="append", default=[])
    parser.add_argument(
        "--oracle-run", action="append", default=[], metavar="WORKLOAD=PATH",
        help="held-out measured run for prediction-error/oracle analysis only",
    )
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
    if args.idle_tool_vm_rss_mib is not None:
        if not args.evaluation_trace:
            parser.error("per-tool memory planning requires --evaluation-trace")
        trace_paths = {
            configured.partition("=")[0]: Path(configured.partition("=")[2])
            for configured in args.evaluation_trace
        }
        payload["per_tool_memory"] = _per_tool_memory_plan(
            kb,
            ToolCallQuery,
            repository=args.repository,
            traces=trace_paths,
            query_ts=max(call.ts_end for call in calls) + 1.0,
            idle_tool_vm_rss_mib=args.idle_tool_vm_rss_mib,
            idle_safety_margin_fraction=args.idle_safety_margin_fraction,
            command_headroom_fraction=args.command_headroom_fraction,
            size_classes_mib=args.tool_memory_size_class_mib,
        )
        oracle_runs: dict[str, Path] = {}
        for configured in args.oracle_run:
            workload, separator, raw_path = configured.partition("=")
            if not separator or not workload or not raw_path:
                parser.error("--oracle-run must use WORKLOAD=PATH")
            oracle_runs[workload] = Path(raw_path)
        if oracle_runs:
            payload["oracle_evaluation"] = _attach_oracle_working_sets(
                payload["per_tool_memory"], oracle_runs,
            )
    prediction = AdmissionPrediction.from_payload(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".next")
    payload.update(prediction.as_payload())
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
