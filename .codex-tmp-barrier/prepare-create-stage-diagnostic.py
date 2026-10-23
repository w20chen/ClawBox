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
case_count = int(sys.argv[5])
concurrency = int(sys.argv[6])
arrival_window_fraction = float(sys.argv[7])
create_concurrency = int(sys.argv[8]) if len(sys.argv) > 8 else 20
dependency_install_concurrency = (
    int(sys.argv[9]) if len(sys.argv) > 9 and sys.argv[9] != "-" else None
)
apt_cache_proxy_url = sys.argv[10] if len(sys.argv) > 10 else None
pypi_cache_index_url = sys.argv[11] if len(sys.argv) > 11 else None

output_root.mkdir(parents=True, exist_ok=False)
spec = yaml.safe_load(source_spec.read_text(encoding="utf-8"))

source_workload = Path(spec["workload"]["input"])
workload = json.loads(source_workload.read_text(encoding="utf-8"))
source_task_count = len(workload["tasks"])
if not 1 <= case_count <= source_task_count:
    raise ValueError(f"case_count must be in [1, {source_task_count}]")
workload["tasks"] = workload["tasks"][:case_count]
workload["selection"] = {
    **workload.get("selection", {}),
    "accepted": case_count,
    "registered": case_count,
    "complete": case_count == source_task_count,
}
workload["diagnostic_selection"] = {
    "kind": "deterministic_prefix",
    "source_task_count": source_task_count,
    "selected_task_count": case_count,
}
workload_path = output_root / "workload-diagnostic.json"
workload_path.write_text(json.dumps(workload, indent=2) + "\n", encoding="utf-8")

source_evidence = source_spec.parent / "action-cost-evidence"
evidence_root = output_root / "action-cost-evidence"
shutil.copytree(source_evidence, evidence_root)

spec["experiment_id"] = experiment_id
spec["workload"]["input"] = str(workload_path)
spec["execution"]["concurrency_levels"] = [concurrency]
spec["execution"]["sandbox_create_concurrency"] = create_concurrency
if dependency_install_concurrency is not None:
    spec["execution"]["dependency_install_concurrency"] = dependency_install_concurrency
else:
    spec["execution"].pop("dependency_install_concurrency", None)
if apt_cache_proxy_url is not None:
    spec["execution"]["apt_cache_proxy_url"] = apt_cache_proxy_url
if pypi_cache_index_url is not None:
    spec["execution"]["pypi_cache_index_url"] = pypi_cache_index_url
spec["execution"]["arrival_schedule"] = "poisson"
spec["execution"]["stagger_interval_seconds"] = 0.0
spec["execution"]["poisson_arrival_window_fraction"] = arrival_window_fraction
spec["execution"]["arm_timeout_seconds"] = 3600
spec["execution"]["arm_finalization_timeout_seconds"] = 600
for key in ("memory_action_costs", "snapshot_residency_costs", "idle_reclaim_costs"):
    provenance = spec["evidence"][key]["provenance"]
    spec["evidence"][key]["provenance"] = provenance.replace(
        str(source_evidence), str(evidence_root)
    )

spec_path = output_root / "experiment.yaml"
spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

code_files = {
    "lifecycle.py": source_root / "clawbox/cube/lifecycle.py",
    "admission.py": source_root / "clawbox/experiments/admission.py",
    "policy.py": source_root / "clawbox/experiments/policy.py",
    "worker.py": source_root / "clawbox/experiments/worker.py",
}
receipt = {
    "schema_version": 1,
    "created_unix_s": time.time(),
    "purpose": "measure real heterogeneous VM-create stage latency past the prior slowdown point",
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
    "diagnostic": {
        "case_selection": "first cases in the frozen full workload",
        "case_count": case_count,
        "source_case_count": source_task_count,
        "concurrency": concurrency,
        "arrival_schedule": "poisson",
        "poisson_arrival_window_fraction": arrival_window_fraction,
        "sandbox_create_concurrency": create_concurrency,
        "dependency_install_concurrency": dependency_install_concurrency,
        "apt_cache_proxy_url": apt_cache_proxy_url,
        "pypi_cache_index_url": pypi_cache_index_url,
        "full_trace_replay": True,
    },
}
(output_root / "diagnostic-receipt.json").write_text(
    json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps(receipt, indent=2))
