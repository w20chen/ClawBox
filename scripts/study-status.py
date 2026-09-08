#!/usr/bin/env python3
"""Show completed baseline outcomes and the latest arm's live replay progress."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path


def status(root: Path) -> str:
    lines = [f"Study: {root}"]
    completed = sorted((root / "arms").glob("*.json"))
    passed = 0
    for path in completed:
        row = json.loads(path.read_text())
        check = row["correctness"]
        count = check.get("completed_sessions", 0)
        total = row["arm"]["concurrency"]
        valid = row["status"] == "succeeded" and count == total and check.get("validation_passed")
        passed += bool(valid)
        lines.append(f"{row['arm']['policy']['name']}: {row['status']}, {count}/{total}, "
                     f"ID join={check.get('native_tool_exact_id_join_rate', 'n/a')}, "
                     f"lost={check.get('native_tool_telemetry_loss_total', 'n/a')}")
    lines.append(f"Finished arm records: {len(completed)}; fully validated: {passed}")
    runs = sorted((root / "runs").glob("*"))
    if not runs:
        return "\n".join(lines + ["No worker output yet."])
    latest = runs[-1]
    events = Counter()
    valid_sessions = set()
    last_progress = None
    for path in (latest / "events").glob("*.jsonl"):
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # A writer may be partway through its last record.
                event = row.get("event")
                events[event] += 1
                if event == "session_complete" and row.get("valid"):
                    valid_sessions.add(row["session_id"])
                if event not in ("memory_sample", "local_cache_reclaim", "local_cache_reclaim_started"):
                    last_progress = row.get("wall_time", last_progress)
    steps = Counter()
    mismatches = 0
    for path in (latest / "model-gateway").glob("*.json"):
        try:
            rows = json.loads(path.read_text())
        except ValueError:
            continue
        steps[len(rows)] += 1
        mismatches += sum(row.get("replay_input_match") is False for row in rows)
    lines += [f"Latest arm: {latest.name}",
              f"Validated sessions: {len(valid_sessions)}; replay mismatches: {mismatches}",
              f"Sessions by model-request count: {dict(sorted(steps.items()))}",
              "(A prefix run includes one final synthetic-stop request.)",
              f"Tool completions: {events['tool_completed']}; pauses/restores: "
              f"{events['sandbox_paused']}/{events['sandbox_restored']}"]
    if last_progress:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_progress.replace("Z", "+00:00"))).total_seconds()
        lines.append(f"Last non-memory progress: {last_progress} ({age:.0f}s ago)")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if not args.directory.is_dir():
        parser.error("study directory does not exist")
    print(status(args.directory))
