#!/usr/bin/env python3
"""Start a paired guest-SSH smoke and checkpoint both VMs once."""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clawbox.replay.lifecycle import FirecrackerConfig, FirecrackerLifecycle

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--runtime", required=True, type=Path)
    p.add_argument("--tool", required=True, type=Path)
    p.add_argument("--inference-store", required=True, type=Path)
    p.add_argument("--pending-timeout-s", type=float, default=15.0)
    p.add_argument("--restore-after-s", type=float, default=5.0)
    p.add_argument("--finish-after-s", type=float, default=8.0)
    a = p.parse_args()
    tool = FirecrackerLifecycle(FirecrackerConfig.from_json(a.tool))
    runtime = FirecrackerLifecycle(FirecrackerConfig.from_json(a.runtime))
    try:
        tool.start(); runtime.start()
        deadline = time.monotonic() + a.pending_timeout_s
        while True:
            if a.inference_store.exists():
                requests = json.loads(a.inference_store.read_text(encoding="utf-8"))
                if any(not item["ready"] for item in requests):
                    break
            if time.monotonic() >= deadline:
                raise TimeoutError("guest did not submit a pending inference request")
            time.sleep(0.02)
        tool.checkpoint_and_evict(); runtime.checkpoint_and_evict()
        time.sleep(a.restore_after_s)
        tool.restore(); runtime.restore()
        time.sleep(a.finish_after_s)
    finally:
        try: tool.close()
        finally: runtime.close()

if __name__ == "__main__": main()
