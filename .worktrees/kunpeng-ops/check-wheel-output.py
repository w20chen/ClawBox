import json
import os
from cubesandbox import Sandbox

vm = Sandbox.create(template=os.environ["CLAWBOX_TOOL_TEMPLATE"], timeout=300,
                    metadata={"clawbox.owner": "wheel-output-check-20260907"},
                    distribution_scope=[os.environ["CUBE_NODE"]])
try:
    result = vm.commands.run("find / -name '*.whl' -not -path '*/proc/*' 2>/dev/null", timeout=30)
    print(json.dumps({"sandbox_id": vm.sandbox_id, "exit_code": result.exit_code,
                      "all_wheels": result.stdout.splitlines()}))
finally:
    vm.kill()
