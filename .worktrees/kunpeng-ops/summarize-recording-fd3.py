#!/usr/bin/env python3
import glob
import json
import os

root = "/data/recording-evidence/session-0000"
for path in glob.glob(root + "/**/*.jsonl", recursive=True):
    for line_number, line in enumerate(open(path, errors="replace"), 1):
        if "/dev/fd/3" not in line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        print("FILE", path, "LINE", line_number)
        print(json.dumps(record, indent=2)[:20000])

bridge = root + "/tool-bridge.jsonl"
print("BRIDGE RECORD COUNT", sum(1 for _ in open(bridge)))
for line_number, line in enumerate(open(bridge), 1):
    record = json.loads(line)
    if int(record.get("exit_code", 0)) != 0:
        print("BRIDGE NONZERO", line_number, json.dumps(record, sort_keys=True))
