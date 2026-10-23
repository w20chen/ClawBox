from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path


MIB = 1024**2


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


result_path = Path(sys.argv[1]).resolve()
workload_path = Path(sys.argv[2]).resolve()
seed_dir = Path(sys.argv[3]).resolve()
output = Path(sys.argv[4]).resolve()

result_bytes = result_path.read_bytes()
result = json.loads(result_bytes)
workload = json.loads(workload_path.read_text(encoding="utf-8"))
manifest = json.loads((seed_dir / "manifest.json").read_text(encoding="utf-8"))
if result.get("status") != "succeeded":
    raise RuntimeError("environment calibration result did not succeed")
correctness = result.get("correctness") or {}
if correctness.get("failed_sessions") != 0 or correctness.get("completed_sessions") != len(workload["tasks"]):
    raise RuntimeError("environment calibration did not complete every workload case")
if correctness.get("native_tool_telemetry_loss_total") != 0:
    raise RuntimeError("environment calibration has Tool telemetry loss")
if manifest.get("schema") != "clawtune.seed.v1" or not manifest["provenance"]["validation"]["passed"]:
    raise RuntimeError("ClawTune cold-start seed is not validated")

tasks = {task["case_id"]: task for task in workload["tasks"]}
assignments = result["performance"]["session_trace_assignment"]
covered_cases = {row["case_id"] for row in assignments}
if covered_cases != set(tasks):
    raise RuntimeError("environment calibration does not cover the workload catalog exactly")

identity_fields = ("template_id", "image_digest", "vcpu", "memory_mib", "architecture")
identities = []
clawtune_revisions = set()
for case_id in sorted(tasks):
    task = tasks[case_id]
    identity = {key: task["sandbox"].get(key) for key in identity_fields}
    if any(value is None for value in identity.values()):
        raise RuntimeError(f"{case_id}: incomplete sandbox identity")
    identities.append(identity)
    clawtune_revisions.add(task["metadata"]["collector_receipt"]["clawtune_revision"])
if len(clawtune_revisions) != 1:
    raise RuntimeError("workload cases use different ClawTune revisions")
unique_identities = list({json.dumps(item, sort_keys=True): item for item in identities}.values())
if len(unique_identities) != len(tasks):
    raise RuntimeError("expected one immutable Tool identity per workload case")

rows = result["performance"]["tool_execution_observations"]
pairs = []
paired_sessions = set()
maintenance_host = []
for row in rows:
    host = row.get("host_cgroup_execution_increment_bytes")
    if isinstance(host, (int, float)) and not isinstance(host, bool) and math.isfinite(host) and host >= 0:
        if row.get("execution_scope") == "backend-maintenance":
            maintenance_host.append(float(host))
        guest_mib = row.get("actual_extra_memory_mib")
        if (row.get("execution_scope") == "agent-tool" and row.get("operation") == "exec"
                and isinstance(guest_mib, (int, float)) and not isinstance(guest_mib, bool)
                and math.isfinite(guest_mib) and guest_mib >= 0):
            pairs.append((float(guest_mib) * MIB, float(host)))
            paired_sessions.add(row["session_id"])
if not pairs or not maintenance_host:
    raise RuntimeError("calibration result has no paired command or maintenance measurements")
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

lattice = json.loads((seed_dir / "clause-lattice-time-kb.json").read_text(encoding="utf-8"))
tool = json.loads((seed_dir / "runtime-tool-resource-kb.json").read_text(encoding="utf-8"))
kb_bytes = json.dumps({"lattice": lattice, "tool": tool}, sort_keys=True).encode()
identities_digest = hashlib.sha256(
    json.dumps(unique_identities, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
payload = {
    "schema": "clawbox_prediction_v2",
    "repository": "*",
    "prediction_scope": "clawtune_cold_start",
    "training_validated": True,
    "training_validation": {
        "cold_start_seed_manifest": str(seed_dir / "manifest.json"),
        "cold_start_seed_validation": manifest["provenance"]["validation"],
        "environment_calibration_result": str(result_path),
        "environment_calibration_result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "covered_case_count": len(covered_cases),
        "paired_command_count": len(pairs),
        "paired_session_count": len(paired_sessions),
        "maintenance_measurement_count": len(maintenance_host),
    },
    "sandbox_identities": unique_identities,
    "sandbox_identities_sha256": identities_digest,
    "host_calibration": host_calibration,
    "non_command_tool_memory_calibration": {
        "target": "host_cgroup_memory_current_increment",
        "sample_count": len(maintenance_host),
        "p90_bytes": quantile(maintenance_host, 0.9),
        "p99_bytes": quantile(maintenance_host, 0.99),
        "recommended_mib": max(1, math.ceil(quantile(maintenance_host, 0.99) / MIB)),
    },
    "kb_sha256": hashlib.sha256(kb_bytes).hexdigest(),
    "clawtune_revision": next(iter(clawtune_revisions)),
    "cold_start_seed_manifest_sha256": hashlib.sha256(
        (seed_dir / "manifest.json").read_bytes()
    ).hexdigest(),
    "commands": [],
}
output.parent.mkdir(parents=True, exist_ok=False)
output.with_suffix(".kb.json").write_bytes(kb_bytes)
output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
print(json.dumps({
    "artifact": str(output),
    "artifact_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    "kb": str(output.with_suffix(".kb.json")),
    "kb_sha256": payload["kb_sha256"],
    "identity_count": len(unique_identities),
    "host_calibration": host_calibration,
    "non_command_tool_memory_calibration": payload["non_command_tool_memory_calibration"],
}, indent=2))
