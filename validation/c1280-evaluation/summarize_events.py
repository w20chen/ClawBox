#!/usr/bin/env python3
"""Print compact progress and lifecycle timing from one experiment event log."""
import collections
import json
import statistics
import sys

events = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
counts = collections.Counter(item.get("event") for item in events)
created = [item for item in events if item.get("event") == "sandbox_created"]
print("events", dict(sorted(counts.items())))
if created:
    span = (max(int(item["wall_time_ns"]) for item in created)
            - min(int(item["wall_time_ns"]) for item in created)) / 1e9
    print("created", len(created), "window_seconds", round(span, 3))
    for role in ("tool", "runtime"):
        values = sorted(item["service_seconds"] for item in created if item["role"] == role)
        if values:
            print(role, "n", len(values), "mean", round(statistics.mean(values), 3),
                  "p50", round(statistics.median(values), 3),
                  "p95", round(values[int(.95 * (len(values) - 1))], 3),
                  "max", round(max(values), 3))
