#!/usr/bin/env python3
import glob
import json
import os

trace = "/home/weitianc/ClawBox/results/paper_replay_20260901_128g_v4/selected-traces/rec-a-enriched.jsonl"
for index, line in enumerate(open(trace)):
    record = json.loads(line)
    rendered = json.dumps(record)
    if "/dev/fd/3" in rendered:
        print("TRACE", index, json.dumps(record, indent=2)[:16000])

print("EVIDENCE FILES")
for path in glob.glob("/data/recording-evidence/session-0000/**/*", recursive=True):
    if os.path.isfile(path):
        print(path, os.path.getsize(path))
