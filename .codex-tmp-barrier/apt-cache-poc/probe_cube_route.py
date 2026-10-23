#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import uuid

from cubesandbox import Config, Sandbox


template, node = sys.argv[1:3]
owner = "clawbox-apt-cache-route-" + uuid.uuid4().hex
sandbox = Sandbox.create(
    template=template,
    timeout=-1,
    lifecycle={"on_timeout": "kill", "auto_resume": False},
    metadata={"clawbox.owner": owner, "clawbox.purpose": "apt-cache-route-poc"},
    distribution_scope=[node],
    allow_internet_access=True,
    config=Config(request_timeout=130),
)
try:
    command = (
        "ip -brief address; ip route; "
        "for h in 10.100.0.1 172.17.0.1 193.124.7.2; do "
        "echo PROBE=$h; timeout 3 bash -c \"</dev/tcp/$h/3142\"; echo STATUS=$?; "
        "done"
    )
    result = sandbox.commands.run(command, timeout=30)
    print(json.dumps({
        "owner": owner,
        "sandbox_id": sandbox.sandbox_id,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }, indent=2))
finally:
    sandbox.kill()
