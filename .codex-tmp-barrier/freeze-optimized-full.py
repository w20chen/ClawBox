from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import yaml


source_spec = Path(sys.argv[1]).resolve()
output_root = Path(sys.argv[2]).resolve()
experiment_id = sys.argv[3]
source_root = Path(sys.argv[4]).resolve()
create_concurrency = int(sys.argv[5]) if len(sys.argv) > 5 else None

output_root.mkdir(parents=True, exist_ok=False)
spec = yaml.safe_load(source_spec.read_text(encoding="utf-8"))

source_workload = Path(spec["workload"]["input"])
workload_path = output_root / "workload-full.json"
shutil.copy2(source_workload, workload_path)

source_evidence = source_spec.parent / "action-cost-evidence"
evidence_root = output_root / "action-cost-evidence"
shutil.copytree(source_evidence, evidence_root)

spec["experiment_id"] = experiment_id
spec["workload"]["input"] = str(workload_path)
spec["mechanisms"]["checkpoint"]["pool_transfer_concurrency"] = 4
spec["mechanisms"]["shared_memory"]["borrow_cooldown_seconds"] = 0.0
if create_concurrency is not None:
    spec["execution"]["sandbox_create_concurrency"] = create_concurrency
spec["environment"]["resources"]["shared_memory_borrow_limit_mib"] = spec[
    "environment"
]["resources"]["warm_memory_capacity_mib"]
for key in ("memory_action_costs", "snapshot_residency_costs", "idle_reclaim_costs"):
    provenance = spec["evidence"][key]["provenance"]
    spec["evidence"][key]["provenance"] = provenance.replace(
        str(source_evidence), str(evidence_root)
    )

spec_path = output_root / "experiment.yaml"
spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

code_files = {
    "admission.py": source_root / "clawbox/experiments/admission.py",
    "policy.py": source_root / "clawbox/experiments/policy.py",
    "worker.py": source_root / "clawbox/experiments/worker.py",
}
receipt = {
    "schema_version": 1,
    "created_unix_s": time.time(),
    "experiment_id": experiment_id,
    "source_spec": str(source_spec),
    "source_spec_sha256": hashlib.sha256(source_spec.read_bytes()).hexdigest(),
    "experiment_spec": str(spec_path),
    "experiment_spec_sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
    "workload": str(workload_path),
    "workload_sha256": hashlib.sha256(workload_path.read_bytes()).hexdigest(),
    "source_root": str(source_root),
    "source_file_sha256": {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in code_files.items()
    },
    "changes": {
        "admission_exact_sampling": (
            "coalesce exact cgroup reads outside the shared admission condition; "
            "reconcile concurrent claims at their full nominal reservation"
        ),
        "borrow_inflight_admission": (
            "do not queue pressure-triggered tool preparation or repeat the VM "
            "candidate scan behind a slow NUMA rebind; re-enter admission and "
            "recheck after the in-flight borrow publishes relief"
        ),
        "pool_transfer_concurrency": 4,
        "borrow_cooldown_seconds": 0.0,
        "shared_memory_borrow_limit_mib": spec["environment"]["resources"][
            "shared_memory_borrow_limit_mib"
        ],
        "sandbox_create_concurrency": spec["execution"][
            "sandbox_create_concurrency"
        ],
    },
}
(output_root / "optimization-receipt.json").write_text(
    json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps(receipt, indent=2))
