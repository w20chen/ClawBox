#!/usr/bin/env python3
"""Serve replayed or real OpenAI-compatible responses to an OpenClaw guest."""
from __future__ import annotations
import argparse
import os
import signal
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clawbox.replay.model_gateway import ModelGateway


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=("replay", "api"), required=True)
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=18081)
    p.add_argument("--trace", type=Path)
    p.add_argument("--time-scale", type=float, default=1.0)
    p.add_argument("--api-base-url")
    p.add_argument("--api-model")
    p.add_argument("--api-key-env", default="OPENAI_API_KEY")
    a = p.parse_args()
    gateway = ModelGateway(
        a.store, mode=a.mode, trace=a.trace, time_scale=a.time_scale,
        upstream_base_url=a.api_base_url,
        upstream_api_key=os.environ.get(a.api_key_env), upstream_model=a.api_model,
    )
    print(gateway.start(a.host, a.port), flush=True)
    stopped = False
    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)
    while not stopped: time.sleep(0.2)
    gateway.close()


if __name__ == "__main__": main()
