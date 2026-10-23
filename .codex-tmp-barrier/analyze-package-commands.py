from __future__ import annotations

import collections
import hashlib
import json
import re
import sys
from pathlib import Path


workload_path = Path(sys.argv[1])
workload = json.loads(workload_path.read_text(encoding="utf-8"))
pip_pattern = re.compile(r"(?:^|[;&|]\s*|\s)(?:\S*/)?(?:pip\d*|python\d*\s+-m\s+pip)\s+install(?:\s|$)")
apt_pattern = re.compile(r"(?:^|[;&|]\s*|\s)(?:sudo\s+\S+\s+)?(?:\S*/)?apt(?:-get)?\s+[^;&|]*install(?:\s|$)")

commands: collections.Counter[str] = collections.Counter()
classes: collections.Counter[str] = collections.Counter()
task_classes: collections.Counter[str] = collections.Counter()
for task in workload.get("tasks") or []:
    seen: set[str] = set()
    trace_path = Path(str(task["replay_trace_reference"]))
    with trace_path.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record.get("record_type") != "span_end":
                continue
            content = ((record.get("output") or {}).get("content") or {})
            for call in content.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") != "exec":
                    continue
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (TypeError, ValueError):
                    continue
                command = arguments.get("command")
                if not isinstance(command, str):
                    continue
                kind = None
                if pip_pattern.search(command):
                    kind = "pip_install"
                elif apt_pattern.search(command):
                    kind = "apt_install"
                if kind is None:
                    continue
                commands[command] += 1
                classes[kind] += 1
                seen.add(kind)
    task_classes.update(seen)

print(json.dumps({
    "schema_version": 1,
    "workload": str(workload_path),
    "tasks": len(workload.get("tasks") or []),
    "package_install_commands": sum(commands.values()),
    "unique_package_install_commands": len(commands),
    "commands_by_class": dict(classes),
    "tasks_by_class": dict(task_classes),
    "commands_repeated_at_least_twice": sum(count >= 2 for count in commands.values()),
    "calls_from_repeated_commands": sum(
        count for count in commands.values() if count >= 2
    ),
    "most_common": [
        {
            "count": count,
            "sha256": hashlib.sha256(command.encode()).hexdigest(),
            "command": command[:500],
        }
        for command, count in commands.most_common(30)
    ],
}, indent=2, sort_keys=True))
