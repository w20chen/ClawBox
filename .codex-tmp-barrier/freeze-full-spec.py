from __future__ import annotations

import hashlib
import json
import shutil
import socket
import sys
import time
from pathlib import Path

import yaml


source_spec = Path(sys.argv[1]).resolve()
output_root = Path(sys.argv[2]).resolve()
experiment_id = sys.argv[3]
action_cost_root = Path(sys.argv[4]).resolve()

output_root.mkdir(parents=True, exist_ok=False)
evidence_root = output_root / "action-cost-evidence"
evidence_root.mkdir()

spec = yaml.safe_load(source_spec.read_text(encoding="utf-8"))
source_workload = Path(spec["workload"]["input"])
workload_path = output_root / "workload-full.json"
shutil.copy2(source_workload, workload_path)

cost_files = {
    "workload-timings.json": action_cost_root / "workload-timings.json",
    "penalty-summary.json": action_cost_root / "penalty-summary.json",
    "borrow-result.json": action_cost_root / "borrow/180cd6fe952c6d33/result.json",
    "snapshot-result.json": action_cost_root / "snapshot/180cd6fe952c6d33/result.json",
}
cost_hashes: dict[str, str] = {}
for name, source in cost_files.items():
    destination = evidence_root / name
    shutil.copy2(source, destination)
    cost_hashes[name] = hashlib.sha256(destination.read_bytes()).hexdigest()

provenance = (
    f"measured on {socket.gethostname()} from {action_cost_root}; immutable copies at "
    f"{evidence_root}; sha256 borrow={cost_hashes['borrow-result.json']}, "
    f"snapshot={cost_hashes['snapshot-result.json']}, "
    f"penalty={cost_hashes['penalty-summary.json']}. Two compute NUMA nodes, 4 GiB "
    "Tool VM, incremental-cow tiered snapshots. Per-GiB means: borrow is mean of "
    "the two node borrow service times divided by 4; warm checkpoint and restore "
    "are first-generation means divided by 4; cold checkpoint is the mean of "
    "second-generation checkpoint plus spill divided by 4; cold restore is the "
    "COLD restore mean divided by 4. Runtime penalty is the paired real pytest "
    "mean nonnegative slowdown per GiB. These are scheduling priors normalized "
    "for the current 3 GiB Tool VM; experiment latency remains measured separately."
)

spec["experiment_id"] = experiment_id
spec["workload"]["input"] = str(workload_path)
spec["environment"]["resources"]["cold_snapshot_root"] = "/data/clawbox-cold"
for key in ("memory_action_costs", "snapshot_residency_costs", "idle_reclaim_costs"):
    spec["evidence"][key]["provenance"] = provenance

spec_path = output_root / "experiment.yaml"
spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
freeze = {
    "schema_version": 1,
    "created_unix_s": time.time(),
    "source_spec": str(source_spec),
    "source_spec_sha256": hashlib.sha256(source_spec.read_bytes()).hexdigest(),
    "source_workload": str(source_workload),
    "source_workload_sha256": hashlib.sha256(source_workload.read_bytes()).hexdigest(),
    "experiment_id": experiment_id,
    "experiment_spec": str(spec_path),
    "experiment_spec_sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
    "workload": str(workload_path),
    "workload_sha256": hashlib.sha256(workload_path.read_bytes()).hexdigest(),
    "cold_snapshot_root": "/data/clawbox-cold",
    "action_cost_source": str(action_cost_root),
    "action_cost_hashes": cost_hashes,
    "action_cost_provenance": provenance,
}
(output_root / "spec-freeze.json").write_text(
    json.dumps(freeze, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps(freeze, indent=2))
