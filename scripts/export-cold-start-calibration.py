#!/usr/bin/env python3
"""Export validated ClawBox observations for a ClawTune cold-start seed."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path


MIB = 1024**2
IDENTITY_FIELDS = ("template_id", "image_digest", "vcpu", "memory_mib", "architecture")


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def export(result_path: Path, receipt_path: Path, workload_path: Path,
           output: Path) -> dict:
    from clawbox.clawtune_integration import use_clawtune
    from clawbox.tuning.__main__ import find_run_datasets
    from clawbox.tuning.clawtune import observation_to_completed_call
    from clawbox.tuning.dataset import build_joined_dataset
    from clawbox.tuning.native import native_clause_observations

    use_clawtune()
    from tool_resource.sdk import _validate_artifact as validate_artifact

    result_path = result_path.resolve()
    receipt_path = receipt_path.resolve()
    workload_path = workload_path.resolve()
    output = output.resolve()
    if output.exists():
        raise ValueError("output observation bundle already exists; use a new path")

    result_bytes = result_path.read_bytes()
    result = json.loads(result_bytes)
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    workload = json.loads(workload_path.read_text(encoding="utf-8"))
    if result.get("status") != "succeeded":
        raise ValueError("environment calibration result did not succeed")
    arm_id = (result.get("arm") or {}).get("arm_id")
    if (receipt.get("status") != "succeeded"
            or receipt.get("cleanup_verified") is not True
            or arm_id not in (receipt.get("arm_ids") or [])):
        raise ValueError("qualification receipt does not verify this result and cleanup")
    correctness = result.get("correctness") or {}
    task_count = len(workload.get("tasks") or [])
    if (correctness.get("failed_sessions") != 0
            or correctness.get("completed_sessions") != task_count):
        raise ValueError("environment calibration did not cleanly complete every workload case")
    if correctness.get("native_tool_telemetry_loss_total") != 0:
        raise ValueError("environment calibration has Tool telemetry loss")
    tasks = {task["case_id"]: task for task in workload["tasks"]}
    assignments = result["performance"]["session_trace_assignment"]
    covered_cases = {row["case_id"] for row in assignments}
    if covered_cases != set(tasks):
        raise ValueError("environment calibration does not cover the workload catalog exactly")

    identities = []
    clawtune_revisions = set()
    for case_id in sorted(tasks):
        task = tasks[case_id]
        identity = {key: task["sandbox"].get(key) for key in IDENTITY_FIELDS}
        if any(value is None for value in identity.values()):
            raise ValueError(f"{case_id}: incomplete sandbox identity")
        identities.append(identity)
        clawtune_revisions.add(
            task["metadata"]["collector_receipt"]["clawtune_revision"]
        )
    if len(clawtune_revisions) != 1:
        raise ValueError("workload cases use different ClawTune revisions")
    unique_identities = list({
        json.dumps(item, sort_keys=True): item for item in identities
    }.values())

    pairs: list[tuple[float, float]] = []
    paired_sessions = set()
    maintenance_host = []
    for row in result["performance"]["tool_execution_observations"]:
        host = row.get("host_cgroup_execution_increment_bytes")
        if (not isinstance(host, (int, float)) or isinstance(host, bool)
                or not math.isfinite(host) or host < 0):
            continue
        if row.get("execution_scope") == "backend-maintenance":
            maintenance_host.append(float(host))
        guest_mib = row.get("actual_extra_memory_mib")
        if (row.get("execution_scope") == "agent-tool" and row.get("operation") == "exec"
                and isinstance(guest_mib, (int, float)) and not isinstance(guest_mib, bool)
                and math.isfinite(guest_mib) and guest_mib >= 0):
            pairs.append((float(guest_mib) * MIB, float(host)))
            paired_sessions.add(row["session_id"])
    if not pairs or not maintenance_host:
        raise ValueError("calibration result has no paired command or maintenance measurements")

    ratios = [host / guest for guest, host in pairs if guest > 0]
    scale = quantile(ratios, 0.9) if ratios else 0.0
    offset = quantile([max(0.0, host - scale * guest) for guest, host in pairs], 0.9)
    host_calibration = {
        "target": "host_cgroup_memory_current_increment",
        "method": "heterogeneous_environment_p90_ratio_plus_p90_residual",
        "sample_count": len(pairs),
        "scale": scale,
        "offset_bytes": offset,
    }

    session_cases = {row["session_id"]: row["case_id"] for row in assignments}
    agent_rows = {
        row["execution_id"]: row
        for row in result["performance"]["tool_execution_observations"]
        if row.get("execution_scope") == "agent-tool" and row.get("operation") == "exec"
    }
    completed_calls = []
    clause_observations = []
    observed_execution_ids = set()
    artifact_root = result_path.parent.parent
    for traces, bridge, resources in find_run_datasets(artifact_root):
        _, trusted = build_joined_dataset(traces, bridge, resource_dir=resources)
        for item in trusted:
            host_row = agent_rows.get(item.execution_id)
            if (host_row is None or item.execution_id in observed_execution_ids
                    or item.cgroup is None or not item.command
                    or not item.complete or item.exit_code != 0):
                continue
            case_id = session_cases.get(host_row.get("session_id"))
            task = tasks.get(case_id)
            if task is None or not task.get("repository"):
                raise ValueError(f"{item.execution_id}: cannot resolve workload repository")
            cgroup_path = resources / f"cgroup-resource-{item.execution_id}.json"
            clause_path = resources / f"clause-telemetry-{item.execution_id}.json"
            if not cgroup_path.is_file() or not clause_path.is_file():
                raise ValueError(
                    f"{item.execution_id}: trusted observation is missing its raw artifacts"
                )
            raw_cgroup = json.loads(cgroup_path.read_text(encoding="utf-8"))
            raw_clause = json.loads(clause_path.read_text(encoding="utf-8"))
            validate_artifact(
                clause_path, raw_clause, expected_repo=task["repository"]
            )
            matching_calls = [
                call for call in raw_clause.get("calls", [])
                if call.get("tool_call_id") == item.execution_id
                and call.get("eligible_for_kb") is True
            ]
            if len(matching_calls) != 1:
                raise ValueError(
                    f"{item.execution_id}: expected one eligible native clause call, "
                    f"found {len(matching_calls)}"
                )
            observed_execution_ids.add(item.execution_id)
            host_increment = host_row.get("host_cgroup_execution_increment_bytes")
            if (isinstance(host_increment, bool)
                    or not isinstance(host_increment, (int, float))
                    or not math.isfinite(host_increment) or host_increment < 0):
                host_increment = None
            completed_calls.append({
                "execution_id": item.execution_id,
                "host_cgroup_execution_increment_bytes": host_increment,
                "call": asdict(observation_to_completed_call(item, task["repository"])),
            })
            for index, observation in enumerate(native_clause_observations(
                task["repository"], matching_calls[0], raw_cgroup,
            )):
                clause_observations.append({
                    "execution_id": item.execution_id,
                    "clause_index": index,
                    "observation": asdict(observation),
                })
    if not completed_calls:
        raise ValueError("calibration result has no trusted completed agent Tool calls")
    memory_calls = [row for row in completed_calls if row["call"]["memory_eligible"]]
    if not memory_calls:
        raise ValueError("calibration result has no valid Cube environment-memory labels")
    memory_clauses = [
        row for row in clause_observations
        if row["observation"]["memory_eligible"]
    ]
    if not memory_clauses:
        raise ValueError("calibration result has no valid Cube clause-memory labels")
    completed_calls.sort(key=lambda row: row["execution_id"])
    clause_observations.sort(key=lambda row: (
        row["execution_id"], row["clause_index"],
        row["observation"]["ts_start"], row["observation"]["ts_end"],
    ))
    identities_digest = hashlib.sha256(
        json.dumps(unique_identities, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    p99_maintenance = quantile(maintenance_host, 0.99)
    payload = {
        "schema": "clawtune.seed_observations.v1",
        "producer": {
            "name": "ClawBox",
            "clawtune_revision": next(iter(clawtune_revisions)),
            "exporter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "validation": {
            "environment_calibration_result": str(result_path),
            "environment_calibration_result_sha256": hashlib.sha256(result_bytes).hexdigest(),
            "qualification_receipt": str(receipt_path),
            "qualification_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "qualification_cleanup_verified": True,
            "covered_case_count": len(covered_cases),
            "paired_command_count": len(pairs),
            "paired_session_count": len(paired_sessions),
            "maintenance_measurement_count": len(maintenance_host),
            "public_prior_scope": "cross_repository_tool_class",
            "public_prior_completed_call_count": len(completed_calls),
            "public_prior_memory_label_count": len(memory_calls),
            "lattice_clause_observation_count": len(clause_observations),
            "lattice_memory_label_count": len(memory_clauses),
            "public_prior_repository_count": len({
                row["call"]["repo"] for row in completed_calls
            }),
            "public_prior_execution_ids_sha256": hashlib.sha256(
                "\n".join(sorted(observed_execution_ids)).encode()
            ).hexdigest(),
        },
        "admission": {
            "sandbox_identities": unique_identities,
            "sandbox_identities_sha256": identities_digest,
            "host_calibration": host_calibration,
            "non_command_tool_memory_calibration": {
                "target": "host_cgroup_memory_current_increment",
                "sample_count": len(maintenance_host),
                "p90_bytes": quantile(maintenance_host, 0.9),
                "p99_bytes": p99_maintenance,
                "recommended_mib": max(1, math.ceil(p99_maintenance / MIB)),
            },
        },
        "observations": {
            "completed_calls": completed_calls,
            "clauses": clause_observations,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "observation_bundle": str(output),
        "observation_bundle_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "identity_count": len(unique_identities),
        "completed_call_count": len(completed_calls),
        "call_memory_label_count": len(memory_calls),
        "clause_observation_count": len(clause_observations),
        "clause_memory_label_count": len(memory_clauses),
        "host_calibration": host_calibration,
        "non_command_tool_memory_calibration": payload["admission"][
            "non_command_tool_memory_calibration"
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True, type=Path,
                        help="successful real-VM arm result JSON covering the workload")
    parser.add_argument("--receipt", required=True, type=Path,
                        help="matching successful qualification receipt with verified cleanup")
    parser.add_argument("--workload", required=True, type=Path,
                        help="workload catalog whose exact Tool identities were measured")
    parser.add_argument("--output", required=True, type=Path,
                        help="new ClawTune observation-bundle JSON path")
    args = parser.parse_args()
    print(json.dumps(export(
        args.result, args.receipt, args.workload, args.output,
    ), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
