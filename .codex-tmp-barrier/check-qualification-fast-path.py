from pathlib import Path

from clawbox.experiments.qualification import (
    _snapshot_sandbox_representatives,
    current_qualification_input_manifest,
    implementation_digest,
)
from clawbox.experiments.spec import load_experiment


spec = load_experiment(Path(
    "/data/clawbox-performance-20261022T154600Z/full/"
    "full-c1280-public-20261022T201300Z/experiment.yaml"
))
observed, representatives = _snapshot_sandbox_representatives(spec)
print({
    "observed_templates": observed,
    "representative_templates": len(representatives),
    "representative_ids": list(representatives),
    "implementation_digest": implementation_digest(),
    "trace_hash_count": len(current_qualification_input_manifest(spec)["traces"]),
})
