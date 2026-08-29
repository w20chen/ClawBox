#!/usr/bin/env python3
"""Start a paired guest-SSH smoke and checkpoint both VMs once."""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clawbox.replay.lifecycle import FirecrackerConfig, FirecrackerLifecycle

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--runtime", required=True, type=Path)
    p.add_argument("--tool", required=True, type=Path)
    p.add_argument("--checkpoint-after-s", type=float, default=0.5)
    p.add_argument("--restore-after-s", type=float, default=2.0)
    p.add_argument("--finish-after-s", type=float, default=8.0)
    a = p.parse_args()
    tool = FirecrackerLifecycle(FirecrackerConfig.from_json(a.tool))
    runtime = FirecrackerLifecycle(FirecrackerConfig.from_json(a.runtime))
    try:
        tool.start(); runtime.start()
        time.sleep(a.checkpoint_after_s)
        tool.checkpoint_and_evict(); runtime.checkpoint_and_evict()
        time.sleep(a.restore_after_s)
        tool.restore(); runtime.restore()
        time.sleep(a.finish_after_s)
    finally:
        try: tool.close()
        finally: runtime.close()

if __name__ == "__main__": main()
