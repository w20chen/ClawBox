#!/usr/bin/env python3
"""Prove that in-guest agent state survives Firecracker eviction/restore."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

from clawbox.replay.guest import VsockRuntimeAgentClient
from clawbox.replay.lifecycle import FirecrackerConfig, FirecrackerLifecycle


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--expected-resident-mib", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error("--cycles must be positive")

    config = FirecrackerConfig.from_json(args.config)
    if config.vsock_uds is None or config.guest_agent_port is None:
        parser.error("config must define vsock_uds and guest_agent_port")
    vm = FirecrackerLifecycle(config)
    agent = VsockRuntimeAgentClient(config.vsock_uds, port=config.guest_agent_port)
    records: list[dict[str, object]] = []
    try:
        start_s = vm.start()
        ready_s = agent.wait_ready(config.boot_timeout_s)
        initial = agent.state()
        expected_resident_bytes = args.expected_resident_mib * 1024 * 1024
        if initial.resident_bytes != expected_resident_bytes:
            raise RuntimeError(
                f"expected {expected_resident_bytes} resident bytes, "
                f"guest reported {initial.resident_bytes}"
            )
        records.append({"event": "started", "start_s": start_s, "ready_s": ready_s,
                        "rss_bytes": vm.rss_bytes(), "state": asdict(initial)})
        expected_nonce = initial.boot_nonce
        for cycle in range(args.cycles):
            request_id = f"smoke-{cycle}"
            before = agent.begin_llm(
                request_id, 30.0,
                {"gpu_id": "sim-gpu-0", "kv_bytes": 64 * 1024 * 1024},
            )
            snapshot_s = vm.checkpoint_and_evict()
            evicted_rss = vm.rss_bytes()
            if evicted_rss != 0:
                raise RuntimeError(f"evicted VM still reports {evicted_rss} RSS bytes")
            # This is the simulated GPU/LLM completion interval.
            time.sleep(0.05)
            restore_s = vm.restore()
            ready_s = agent.wait_ready(config.boot_timeout_s)
            restored = agent.assert_inflight(request_id, expected_nonce)
            if restored.resident_bytes != expected_resident_bytes:
                raise RuntimeError("guest working set metadata did not survive restore")
            completed = agent.complete_llm(request_id)
            restored_rss = vm.rss_bytes()
            after_tool = agent.tool_completed(f"tool-{cycle}", 0)
            records.append({
                "event": "cycle", "cycle": cycle, "snapshot_s": snapshot_s,
                "restore_s": restore_s, "ready_s": ready_s,
                "evicted_rss_bytes": evicted_rss,
                "restored_rss_bytes_before_tool": restored_rss,
                "before": asdict(before), "restored": asdict(restored),
                "completed": asdict(completed),
                "after_tool": asdict(after_tool), "after_tool_rss_bytes": vm.rss_bytes(),
            })
        final = agent.state()
        if final.boot_nonce != expected_nonce or final.turn != args.cycles:
            raise RuntimeError(f"unexpected final guest state: {final}")
        records.append({"event": "success", "state": asdict(final)})
    finally:
        vm.close()

    rendered = json.dumps(records, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
