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
        "n": len(values), "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 0.5), "p90": percentile(values, 0.9),
        "p95": percentile(values, 0.95), "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


path = Path(sys.argv[1])
start, end = parse_time(sys.argv[2]), parse_time(sys.argv[3])
rows: list[tuple[dt.datetime, float]] = []
with path.open(encoding="utf-8") as stream:
    for line in stream:
        try:
            event = json.loads(line)
            timestamp = parse_time(event["@timestamp"])
            content = event.get("LogContent", "")
        except (ValueError, KeyError, TypeError):
            continue
        if not start <= timestamp <= end or not content.startswith("CreateSandbox_rsp:"):
            continue
        try:
            response = json.loads(content.split(":", 1)[1])
            milliseconds = float(response["ext_info"]["cube-e2e"])
        except (ValueError, KeyError, TypeError):
            continue
        rows.append((timestamp, milliseconds / 1000.0))

by_minute: dict[str, list[float]] = collections.defaultdict(list)
for timestamp, seconds in rows:
    by_minute[timestamp.replace(second=0, microsecond=0).isoformat()].append(seconds)
print(json.dumps({
    "path": str(path), "start": start.isoformat(), "end": end.isoformat(),
    "cube_e2e_seconds": summary([seconds for _, seconds in rows]),
    "by_minute": {minute: summary(values) for minute, values in sorted(by_minute.items())},
}, indent=2))
