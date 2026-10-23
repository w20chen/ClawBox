import json
import time

from clawbox.cube import CubeSandboxClient


owner = "qualification-snapshot-826b8b31ec9d"
experiment = "full-c1280-public-20261022t201300z"
policy = "qualification-snapshot-roundtrip"
client = CubeSandboxClient(cleanup_workers=8)
before = client.list_owned_sandboxes(owner)
unexpected = [
    row for row in before
    if (row.get("metadata") or {}).get("clawbox.experiment") != experiment
    or (row.get("metadata") or {}).get("clawbox.policy-name") != policy
]
if unexpected:
    raise RuntimeError("ownership scope contains unexpected metadata; refusing cleanup")
started = time.time()
client.kill_owned_sandboxes(owner)
after = client.list_owned_sandboxes(owner)
print(json.dumps({
    "schema_version": 1,
    "owner_id": owner,
    "experiment_id": experiment,
    "policy_name": policy,
    "started_unix_s": started,
    "finished_unix_s": time.time(),
    "before_count": len(before),
    "before_states": {
        state: sum(row.get("state") == state for row in before)
        for state in sorted({str(row.get("state")) for row in before})
    },
    "after_count": len(after),
}, indent=2))
