#!/usr/bin/env python3
"""Run the host endpoint used by a guest-driven replay arm."""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clawbox.replay.inference_service import ReplayInferenceService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--time-scale", type=float, default=1.0)
    args = parser.parse_args()
    service = ReplayInferenceService(args.store, time_scale=args.time_scale)
    url = service.start(args.host, args.port)
    print(url, flush=True)
    running = True
    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        time.sleep(0.2)
    service.close()


if __name__ == "__main__":
    main()
