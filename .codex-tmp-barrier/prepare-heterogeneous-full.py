from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


source_spec = Path(sys.argv[1])
reference_full_spec = Path(sys.argv[2])
prediction_artifact = Path(sys.argv[3])
output_root = Path(sys.argv[4])
experiment_id = sys.argv[5]
concurrency = int(sys.argv[6])
output_root.mkdir(parents=True, exist_ok=False)

spec = yaml.safe_load(source_spec.read_text(encoding="utf-8"))
reference = yaml.safe_load(reference_full_spec.read_text(encoding="utf-8"))
catalog = Path(spec["workload"]["input"])
workload = json.loads(catalog.read_text(encoding="utf-8"))
for task in workload["tasks"]:
    trace = Path(task["replay_trace_reference"])
    if not trace.is_absolute():
        task["replay_trace_reference"] = str((catalog.parent / trace).resolve())
    task["snapshot_reuse_probability"] = 1.0
workload_path = output_root / "workload-full.json"
workload_path.write_text(json.dumps(workload, indent=2), encoding="utf-8")

artifact = json.loads(prediction_artifact.read_text(encoding="utf-8"))
spec["experiment_id"] = experiment_id
spec["workload"].update(
    input=str(workload_path), cases=[], session_assignment="round_robin",
)
spec["inference"]["configuration"] = {
    "model": "example-model",
    "time_scale": 1.0,
    "runtime_startup_timeout_seconds": 300,
}
spec["environment"]["resources"]["cold_snapshot_root"] = str(output_root / "cold")
spec["evidence"].update(
    prediction_artifact=str(prediction_artifact.resolve()),
    memory_action_costs=reference["evidence"]["memory_action_costs"],
    snapshot_residency_costs=reference["evidence"]["snapshot_residency_costs"],
    idle_reclaim_costs=reference["evidence"]["idle_reclaim_costs"],
)
spec["evidence"]["memory_action_costs"]["provenance"] = (
    "provisional capability qualification seed; replace before Full run: "
    + spec["evidence"]["memory_action_costs"]["provenance"]
)
spec["evidence"]["snapshot_residency_costs"]["provenance"] = (
    "provisional capability qualification seed; replace before Full run: "
    + spec["evidence"]["snapshot_residency_costs"]["provenance"]
)
spec["evidence"]["idle_reclaim_costs"]["provenance"] = (
    "provisional capability qualification seed; replace before Full run: "
    + spec["evidence"]["idle_reclaim_costs"]["provenance"]
)
spec["measurement"].update(memory_sample_interval_seconds=1.0)
spec["mechanisms"] = reference["mechanisms"]
recommended = artifact["non_command_tool_memory_calibration"]["recommended_mib"]
spec["mechanisms"]["admission"]["static_tool_memory_mib"] = recommended
spec["mechanisms"]["admission"]["non_command_tool_memory_mib"] = recommended
spec["conditions"] = {"prediction_source": "cold-start-kb"}
spec["comparison"] = {"recipe": "custom", "arms": [{"name": "full", "override": {}}]}
spec["execution"].update(
    concurrency_levels=[concurrency],
    sandbox_create_concurrency=20,
    randomized_order=True,
    random_seed=20261023,
    arm_timeout_seconds=18000,
    arm_finalization_timeout_seconds=600,
    command_timeout_seconds=1200,
    arrival_schedule="poisson",
    stagger_interval_seconds=0.0,
    poisson_arrival_window_fraction=2.0,
)
spec_path = output_root / "experiment.yaml"
spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
print(json.dumps({
    "spec": str(spec_path),
    "workload": str(workload_path),
    "task_count": len(workload["tasks"]),
    "concurrency": concurrency,
    "non_command_tool_memory_mib": recommended,
    "prediction_artifact": str(prediction_artifact),
    "costs": "provisional-old-4gib-for-capability-qualification-only",
}, indent=2))
