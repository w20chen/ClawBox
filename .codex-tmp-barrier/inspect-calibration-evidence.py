from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
performance = result["performance"]
rows = performance.get("tool_execution_observations", [])
sessions = performance.get("session_trace_assignment", [])
agent_rows = [row for row in rows if row.get("execution_scope") == "agent-tool"]
paired = [row for row in agent_rows if row.get("operation") == "exec"
          and row.get("actual_extra_memory_mib") is not None
          and row.get("host_cgroup_execution_increment_bytes") is not None]
print(json.dumps({
    "row_count": len(rows),
    "row_keys": sorted({key for row in rows for key in row}),
    "sample_rows": rows[:3],
    "session_count": len(sessions),
    "session_keys": sorted({key for row in sessions for key in row}),
    "scope_counts": Counter(str(row.get("execution_scope")) for row in rows),
    "operation_counts": Counter(str(row.get("operation")) for row in rows),
    "agent_row_count": len(agent_rows),
    "paired_count": len(paired),
    "paired_session_count": len({row["session_id"] for row in paired}),
    "paired_sample_rows": paired[:5],
}, indent=2, default=str))
