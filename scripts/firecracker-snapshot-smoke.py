#!/usr/bin/env python3
"""Exercise two Firecracker snapshot/evict/restore cycles from one config."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from clawbox.replay.lifecycle import FirecrackerConfig, FirecrackerLifecycle


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--settle-seconds", type=float, default=2.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    vm = FirecrackerLifecycle(FirecrackerConfig.from_json(args.config))
    events: list[dict[str, float | int | str]] = []

    def record(event: str, duration_s: float) -> None:
        events.append({
            "event": event,
            "duration_s": duration_s,
            "rss_bytes": vm.rss_bytes(),
            "wall_time_s": time.time(),
        })

    try:
        record("start", vm.start())
        time.sleep(args.settle_seconds)
        record("checkpoint_1", vm.checkpoint_and_evict())
        record("restore_1", vm.restore())
        time.sleep(args.settle_seconds)
        record("checkpoint_2", vm.checkpoint_and_evict())
        record("restore_2", vm.restore())
        time.sleep(args.settle_seconds)
    finally:
        vm.close()
    payload = {"ok": True, "events": events}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
