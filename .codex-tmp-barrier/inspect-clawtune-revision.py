from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from clawbox.clawtune_integration import source_revision
from clawbox.experiments.qualification import host_profile_for
from clawbox.experiments.spec import load_experiment
from clawbox.lab import apply_environment, sdk


spec = load_experiment(Path(sys.argv[1]))
profile = host_profile_for(spec)
apply_environment(profile)
root = Path(sys.argv[2])
result = {
    "source_root": str(root),
    "source_revision": source_revision(root),
    "roles": {},
}
for role in ("runtime", "sandbox"):
    item = sdk()[1].get(profile[role]["template_id"])
    containers = (item.create_request or {}).get("containers") or []
    environment = {
        entry["key"]: entry.get("value")
        for container in containers
        for entry in container.get("envs", [])
    }
    result["roles"][role] = {
        "template_id": profile[role]["template_id"],
        "clawtune_revision": environment.get("CLAWTUNE_REVISION"),
    }
print(json.dumps(result, indent=2, sort_keys=True))
