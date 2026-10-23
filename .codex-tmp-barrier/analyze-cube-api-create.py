#!/usr/bin/env python3
from __future__ import annotations

import collections
import datetime as dt
import json
import math
import statistics
import sys
from pathlib import Path


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summary(values: list[float]) -> dict:
    return {
        "n": len(values),
        "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 0.5),
        "p90": percentile(values, 0.9),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


path = Path(sys.argv[1])
start = parse_time(sys.argv[2]) if len(sys.argv) > 2 else None
end = parse_time(sys.argv[3]) if len(sys.argv) > 3 else None
pending: dict[str, collections.deque[dt.datetime]] = collections.defaultdict(collections.deque)
completed: list[tuple[dt.datetime, str, float]] = []
request_count = 0
with path.open(encoding="utf-8") as stream:
    for line in stream:
        try:
            event = json.loads(line)
            timestamp = parse_time(event["timestamp"])
        except (ValueError, KeyError, TypeError):
            continue
        if start is not None and timestamp < start:
            continue
        if end is not None and timestamp > end:
            continue
        template = str(event.get("template_id") or "")
        if event.get("event") == "api.request" and event.get("handler") == "create_sandbox":
            if template:
                pending[template].append(timestamp)
                request_count += 1
        elif event.get("event") == "sandbox.created" and template and pending[template]:
            requested = pending[template].popleft()
            completed.append((timestamp, template, (timestamp - requested).total_seconds()))

values = [row[2] for row in completed]
by_minute: dict[str, list[float]] = collections.defaultdict(list)
for completed_at, _template, seconds in completed:
    by_minute[completed_at.replace(second=0, microsecond=0).isoformat()].append(seconds)
result = {
    "path": str(path),
    "start": start.isoformat() if start else None,
    "end": end.isoformat() if end else None,
    "requests": request_count,
    "completed": len(completed),
    "unmatched_requests": sum(len(queue) for queue in pending.values()),
    "latency_seconds": summary(values),
    "by_minute": {minute: summary(rows) for minute, rows in sorted(by_minute.items())},
    "slowest": [
        {"completed_at": completed_at.isoformat(), "template_id": template, "seconds": seconds}
        for completed_at, template, seconds in sorted(completed, key=lambda row: row[2], reverse=True)[:20]
    ],
}
print(json.dumps(result, indent=2))
