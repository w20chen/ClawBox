#!/usr/bin/env python3
"""Export a multi-environment ClawTune cold-start admission artifact."""

from __future__ import annotations

import argparse
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
           seed_dir: Path, output: Path) -> dict:
    from clawbox.clawtune_integration import use_clawtune
    from clawbox.tuning.__main__ import find_run_datasets
    from clawbox.tuning.clawtune import observation_to_completed_call
    from clawbox.tuning.dataset import build_joined_dataset

    use_clawtune()
    from tool_resource.runtime_kb import RuntimeToolResourceKB

    result_path = result_path.resolve()
    receipt_path = receipt_path.resolve()
    workload_path = workload_path.resolve()
    seed_dir = seed_dir.resolve()
    output = output.resolve()
    if output.exists() or output.with_suffix(".kb.json").exists():
        raise ValueError("output artifact or matching KB already exists; use a new path")

    result_bytes = result_path.read_bytes()
    result = json.loads(result_bytes)
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    workload = json.loads(workload_path.read_text(encoding="utf-8"))
    manifest_path = seed_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
    if (manifest.get("schema") != "clawtune.seed.v1"
            or not manifest["provenance"]["validation"]["passed"]):
        raise ValueError("ClawTune cold-start seed is not validated")

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
    observed_execution_ids = set()
    artifact_root = result_path.parent.parent
    for traces, bridge, resources in find_run_datasets(artifact_root):
        _, trusted = build_joined_dataset(traces, bridge, resource_dir=resources)
        for item in trusted:
            host_row = agent_rows.get(item.execution_id)
            if (host_row is None or item.execution_id in observed_execution_ids
                    or not item.command or not item.complete or item.exit_code != 0):
                continue
            case_id = session_cases.get(host_row.get("session_id"))
            task = tasks.get(case_id)
            if task is None or not task.get("repository"):
                raise ValueError(f"{item.execution_id}: cannot resolve workload repository")
            observed_execution_ids.add(item.execution_id)
            completed_calls.append(
                observation_to_completed_call(item, task["repository"])
            )
    if not completed_calls:
        raise ValueError("calibration result has no trusted completed agent Tool calls")
    memory_calls = [call for call in completed_calls if call.memory_eligible]
    if not memory_calls:
        raise ValueError("calibration result has no valid Cube environment-memory labels")

    # ClawTune's shipped ToolKB intentionally has no cross-repository public
    # prior.  Fit its official immutable public layer from this workload-wide
    # qualification so unseen commands have measured evidence rather than an
    # invented zero.  Repository and exact-command identities are deliberately
    # omitted from this layer; the Full run remains a cold-command evaluation.
    runtime = RuntimeToolResourceKB.fit_public(completed_calls)
    runtime.freeze()
    tool = runtime.to_json_obj()
    RuntimeToolResourceKB.from_json_obj(tool)
    lattice = json.loads(
        (seed_dir / "clause-lattice-time-kb.json").read_text(encoding="utf-8")
    )
    kb_bytes = json.dumps({"lattice": lattice, "tool": tool}, sort_keys=True).encode()
    identities_digest = hashlib.sha256(
        json.dumps(unique_identities, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    p99_maintenance = quantile(maintenance_host, 0.99)
    payload = {
        "schema": "clawbox_prediction_v2",
        "repository": "*",
        "prediction_scope": "clawtune_cold_start",
        "training_validated": True,
        "training_validation": {
            "cold_start_seed_manifest": str(manifest_path),
            "cold_start_seed_validation": manifest["provenance"]["validation"],
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
            "public_prior_repository_count": len({call.repo for call in completed_calls}),
            "public_prior_execution_ids_sha256": hashlib.sha256(
                "\n".join(sorted(observed_execution_ids)).encode()
            ).hexdigest(),
            "exporter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
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
        "kb_sha256": hashlib.sha256(kb_bytes).hexdigest(),
        "clawtune_revision": next(iter(clawtune_revisions)),
        "cold_start_seed_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "tool_kb_source": "validated_heterogeneous_qualification_public_prior",
        "commands": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".kb.json").write_bytes(kb_bytes)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "artifact": str(output),
        "artifact_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "kb": str(output.with_suffix(".kb.json")),
        "kb_sha256": payload["kb_sha256"],
        "identity_count": len(unique_identities),
        "host_calibration": host_calibration,
        "non_command_tool_memory_calibration": payload[
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
    parser.add_argument("--seed-dir", required=True, type=Path,
                        help="validated ClawTune bootstrap-v1 directory")
    parser.add_argument("--output", required=True, type=Path,
                        help="new artifact .json path; a sibling .kb.json is also written")
    args = parser.parse_args()
    print(json.dumps(export(
        args.result, args.receipt, args.workload, args.seed_dir, args.output,
    ), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
