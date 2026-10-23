from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


source_spec = Path(sys.argv[1])
output_root = Path(sys.argv[2])
experiment_id = sys.argv[3]
output_root.mkdir(parents=True, exist_ok=False)

spec = yaml.safe_load(source_spec.read_text(encoding="utf-8"))
catalog = Path(spec["workload"]["input"])
workload = json.loads(catalog.read_text(encoding="utf-8"))
task = workload["tasks"][0]
trace = Path(task["replay_trace_reference"])
if not trace.is_absolute():
    task["replay_trace_reference"] = str((catalog.parent / trace).resolve())
workload["tasks"] = [task]
workload_path = output_root / "workload-1.json"
workload_path.write_text(json.dumps(workload, indent=2), encoding="utf-8")

spec["experiment_id"] = experiment_id
spec["workload"].update(
    input=str(workload_path), cases=[], session_assignment="unique",
)
spec["environment"]["sandbox"] = task["sandbox"]
spec["environment"]["resources"]["cold_snapshot_root"] = str(output_root / "cold")
spec["evidence"]["template_base_sharing_artifact"] = None
spec["measurement"]["validation"]["command"] = "true"
spec["execution"]["concurrency_levels"] = [1]
spec["execution"]["arrival_schedule"] = "burst"
spec["execution"]["poisson_arrival_window_fraction"] = 2.0
spec_path = output_root / "experiment.yaml"
spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
print(json.dumps({"spec": str(spec_path), "case_id": task["case_id"]}))
