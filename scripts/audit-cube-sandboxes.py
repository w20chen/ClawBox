#!/usr/bin/env python3
"""Print concise CubeSandbox inventory without exposing credentials."""
import json

from cubesandbox import Sandbox, Template


for item in Sandbox.list_v2():
    print(json.dumps({
        "id": item.get("sandboxID") or item.get("sandbox_id"),
        "state": item.get("state"),
        "template": item.get("templateID") or item.get("template_id"),
        "metadata": item.get("metadata") or {},
    }, sort_keys=True))

for item in Template.list():
    print(json.dumps({
        "template_id": item.template_id, "name": item.name,
        "status": item.status, "cpu_count": item.cpu_count,
        "memory_mb": item.memory_mb, "image": item.image_info,
    }, sort_keys=True, default=str))
