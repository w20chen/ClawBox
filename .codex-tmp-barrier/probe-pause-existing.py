import json
import sys

from cubesandbox import Sandbox
from clawbox.cube import CubeSandboxClient


sandbox_id = sys.argv[1]
path = sys.argv[2]
sandbox = Sandbox.connect(sandbox_id)
manifest = CubeSandboxClient.pause_sandbox(
    sandbox,
    tier="warm",
    memory_snapshot_path=path,
    generation=1,
    snapshot_mechanism="incremental-cow",
)
print(json.dumps(manifest, indent=2), flush=True)
