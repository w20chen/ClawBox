import json
from pathlib import Path

import yaml


goal = Path("/data/clawbox-acceptance-20261022T102744Z")
repair = goal / "repairs/qualification-bounded-v2-20261022T144000Z"
smoke = repair / "real-vm-smoke"
smoke.mkdir(parents=True, exist_ok=False)

catalog = Path(
    "/mnt/clawbox-data/artifacts/swe-readiness-20261021/"
    "environment-catalog-20261022/snapshot-20261022T104346.199284Z/"
    "workload-ready.json"
)
workload = json.loads(catalog.read_text())
tasks = workload["tasks"][:5]
for task in tasks:
    replay = Path(task["replay_trace_reference"])
    if not replay.is_absolute():
        task["replay_trace_reference"] = str((catalog.parent / replay).resolve())
workload["tasks"] = tasks
workload_path = smoke / "workload-5.json"
workload_path.write_text(json.dumps(workload, indent=2))

source_spec = goal / "runs/swe-all503-c16-barrier-v2-20261022t140511z/experiment.yaml"
spec = yaml.safe_load(source_spec.read_text())
spec["experiment_id"] = "qualification-bound-real-vm-smoke-20261022t144000z"
spec["workload"].update(input=str(workload_path), cases=[])
spec["environment"]["sandbox"] = tasks[0]["sandbox"]
spec["environment"]["resources"]["cold_snapshot_root"] = str(smoke / "cold")
spec["measurement"]["validation"]["command"] = "true"
spec["execution"]["concurrency_levels"] = [5]
spec_path = smoke / "experiment.yaml"
spec_path.write_text(yaml.safe_dump(spec, sort_keys=False))
print(json.dumps({"spec": str(spec_path), "cases": len(tasks)}))
