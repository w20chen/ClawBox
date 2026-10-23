#!/usr/bin/env python3
import json
import sys
from pathlib import Path


for line in Path(sys.argv[1]).read_text().splitlines():
    record = json.loads(line)
    if record.get("record_type") != "span_end" or record.get("kind") != "llm":
        continue
    output = record.get("output") or {}
    content = output.get("content") or {}
    for call in content.get("tool_calls") or []:
        function = call.get("function") or {}
        if function.get("name") != "exec":
            continue
        arguments = function.get("arguments") or "{}"
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = {"raw_arguments": arguments}
        print(json.dumps({"span_id": record.get("span_id"), **parsed}, ensure_ascii=False))
