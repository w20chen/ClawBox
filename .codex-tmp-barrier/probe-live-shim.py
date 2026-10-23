import sys
import time

from cubesandbox import Sandbox


template = sys.argv[1]
node = sys.argv[2]
owner = sys.argv[3]
sandbox = Sandbox.create(
    template=template,
    timeout=600,
    metadata={"clawbox.owner": owner},
    distribution_scope=[node],
)
print(sandbox.sandbox_id, flush=True)
try:
    time.sleep(120)
finally:
    sandbox.kill()
