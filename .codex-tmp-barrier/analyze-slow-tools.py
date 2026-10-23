from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


event_path = Path(sys.argv[1])
workload_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 30

rows: dict[str, dict] = {}
with event_path.open(encoding="utf-8") as stream:
    for line in stream:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        execution_id = str(event.get("execution_id") or "")
        if not execution_id:
            continue
        name = event.get("event")
        row = rows.setdefault(execution_id, {"execution_id": execution_id})
        if name == "tool_admitted":
            row.update({
                "session_id": event.get("session_id"),
                "command_sha256": event.get("command_sha256"),
                "execution_scope": event.get("execution_scope"),
                "operation": event.get("operation"),
                "admitted_unix_s": int(event["wall_time_ns"]) / 1e9,
            })
        elif name == "tool_execution_progress":
            phase = event.get("phase")
            if phase in {"start", "end"}:
                row[f"execution_{phase}_unix_s"] = int(event["wall_time_ns"]) / 1e9
        elif name == "tool_completed":
            row["completed_unix_s"] = int(event["wall_time_ns"]) / 1e9
            row["exit_code"] = event.get("exit_code")

completed = []
for row in rows.values():
    if "execution_start_unix_s" not in row or "execution_end_unix_s" not in row:
        continue
    row["execution_seconds"] = (
        row["execution_end_unix_s"] - row["execution_start_unix_s"]
    )
    if "admitted_unix_s" in row:
        row["admitted_to_start_seconds"] = (
            row["execution_start_unix_s"] - row["admitted_unix_s"]
        )
    completed.append(row)

slowest = sorted(
    completed, key=lambda row: float(row["execution_seconds"]), reverse=True,
)[:limit]
target_hashes = {
    str(row.get("command_sha256")) for row in slowest if row.get("command_sha256")
}

commands: dict[str, list[dict]] = {}
if workload_path is not None:
    workload = json.loads(workload_path.read_text(encoding="utf-8"))
    for task_index, task in enumerate(workload.get("tasks") or []):
        trace_path = Path(str(task.get("replay_trace_reference") or ""))
        if not trace_path.is_file():
            continue
        with trace_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
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
                    digest = hashlib.sha256(command.encode()).hexdigest()
                    if digest not in target_hashes:
                        continue
                    commands.setdefault(digest, []).append({
                        "task_index": task_index,
                        "case_id": task.get("case_id"),
                        "trace_path": str(trace_path),
                        "command": command,
                    })

for row in slowest:
    digest = str(row.get("command_sha256") or "")
    if digest in commands:
        row["recorded_commands"] = commands[digest]

print(json.dumps({
    "event_path": str(event_path),
    "completed_execution_count": len(completed),
    "slowest": slowest,
}, indent=2, sort_keys=True))
