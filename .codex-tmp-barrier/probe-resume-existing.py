import json
import sys

from cubesandbox import Sandbox


sandbox = Sandbox.connect(sys.argv[1], snapshot_mechanism="incremental-cow")
result = sandbox.commands.run("printf tiered-restore-ok", timeout=30)
print(json.dumps({
    "sandbox_id": sandbox.sandbox_id,
    "exit_code": result.exit_code,
    "stdout": result.stdout,
    "stderr": result.stderr,
}), flush=True)
sandbox.kill()
