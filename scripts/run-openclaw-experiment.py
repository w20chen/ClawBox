#!/usr/bin/env python3
"""Run OpenClaw+ClawTune in the Runtime VM with SSH tools and one model gateway."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clawbox.replay.lifecycle import FirecrackerConfig, FirecrackerLifecycle
from clawbox.replay.model_gateway import ModelGateway


def wait_tcp(host: str, port: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5): return
        except OSError: time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {host}:{port}")


def complete(log: Path) -> tuple[bool, int | None]:
    if not log.exists(): return False, None
    for line in reversed(log.read_text(errors="replace").splitlines()):
        if line.startswith('{"ok":') and "openclaw_exit_code" in line:
            value = json.loads(line)
            return True, int(value["openclaw_exit_code"])
    return False, None


def request_summaries(gateway: ModelGateway) -> list[dict]:
    summaries = []
    for item in gateway.records():
        encoded = item.pop("response_b64", "")
        item["response_bytes"] = (len(encoded) * 3 // 4 - encoded.count("=")) if encoded else 0
        summaries.append(item)
    return summaries


def ssh_validate(spec: dict, command: str) -> str:
    marker = b"\n__CLAWBOX_VALIDATION_EXIT__:"
    remote = f"{command}; status=$?; printf '\\n__CLAWBOX_VALIDATION_EXIT__:%d\\n' \"$status\""
    process = subprocess.Popen([
        "ssh", "-p", "2222", "-i", spec["identity"], "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={spec['known_hosts']}",
        f"executor@{spec['tool_host']}", remote,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    before, found, after = stdout.rpartition(marker)
    if not found:
        raise RuntimeError(f"validation did not return a completion marker: {stderr.decode(errors='replace')}")
    exit_text = after.splitlines()[0]
    if exit_text != b"0":
        raise RuntimeError(f"validation command exited with {exit_text.decode(errors='replace')}")
    return hashlib.sha256(before).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("manifest", type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--mode", choices=("resident", "snapshot"), required=True)
    p.add_argument("--inference", choices=("replay", "api"), required=True)
    p.add_argument("--trace", type=Path)
    p.add_argument("--time-scale", type=float, default=1.0)
    p.add_argument("--api-base-url"); p.add_argument("--api-model")
    p.add_argument("--api-key-env", default="OPENAI_API_KEY")
    p.add_argument("--validation-command", default="cd /testbed && git diff --binary --no-ext-diff HEAD")
    p.add_argument("--timeout-s", type=float, default=900)
    a = p.parse_args()
    raw = json.loads(a.manifest.read_text())
    a.output.mkdir(parents=True, exist_ok=False)
    lifecycles: list[FirecrackerLifecycle] = []
    lifecycle_lock = threading.Lock()
    samples: list[dict] = []
    stop = threading.Event()
    started = time.monotonic()
    def sample() -> None:
        while not stop.wait(0.1):
            with lifecycle_lock: rss = sum(item.rss_bytes() for item in lifecycles)
            samples.append({"elapsed_s": time.monotonic() - started, "firecracker_rss_bytes": rss})
    sampler = threading.Thread(target=sample, daemon=True); sampler.start()

    def run_one(index: int, spec: dict) -> dict:
        gateway = ModelGateway(
            Path(spec["store"]), mode=a.inference, trace=a.trace, time_scale=a.time_scale,
            upstream_base_url=a.api_base_url,
            upstream_api_key=os.environ.get(a.api_key_env), upstream_model=a.api_model,
        )
        tool_config, runtime_config = FirecrackerConfig.from_json(Path(spec["tool"])), FirecrackerConfig.from_json(Path(spec["runtime"]))
        tool, runtime = FirecrackerLifecycle(tool_config), FirecrackerLifecycle(runtime_config)
        with lifecycle_lock: lifecycles.extend([tool, runtime])
        snapshots = 0; snapshot_s = restore_s = 0.0
        gateway.start(spec["gateway_host"], 18081)
        deadline = time.monotonic() + a.timeout_s
        try:
            tool.start(); wait_tcp(spec["tool_host"], 2222, 30); runtime.start()
            processed: set[str] = set()
            while time.monotonic() < deadline:
                finished, exit_code = complete(Path(runtime_config.log_path))
                if finished:
                    if exit_code != 0: raise RuntimeError(f"OpenClaw exited with {exit_code}")
                    return {"session": index, "snapshots": snapshots,
                            "snapshot_s": snapshot_s, "restore_s": restore_s,
                            "validation_sha256": ssh_validate(spec, a.validation_command),
                            "model_requests": request_summaries(gateway)}
                runtime_exit = runtime.process_exit_code()
                tool_exit = tool.process_exit_code()
                if runtime_exit is not None:
                    raise RuntimeError(f"Runtime VM exited unexpectedly ({runtime_exit})")
                if tool_exit is not None:
                    raise RuntimeError(f"Tool VM exited unexpectedly ({tool_exit})")
                pending = [item for item in gateway.records()
                           if not item["ready"] and item["request_id"] not in processed]
                if a.mode == "snapshot" and pending:
                    request_id = pending[0]["request_id"]
                    snapshot_s += tool.checkpoint_and_evict()
                    snapshot_s += runtime.checkpoint_and_evict()
                    while time.monotonic() < deadline:
                        current = {item["request_id"]: item for item in gateway.records()}
                        if current[request_id]["ready"]: break
                        time.sleep(0.02)
                    restore_s += tool.restore(); wait_tcp(spec["tool_host"], 2222, 30)
                    restore_s += runtime.restore(); processed.add(request_id); snapshots += 1
                else: time.sleep(0.05)
            raise TimeoutError("OpenClaw experiment timed out")
        finally:
            try: runtime.close()
            finally:
                try: tool.close()
                finally: gateway.close()

    results, failures = [], []
    try:
        with ThreadPoolExecutor(max_workers=len(raw["sessions"])) as pool:
            futures = {pool.submit(run_one, i, s): i for i, s in enumerate(raw["sessions"])}
            for future in as_completed(futures):
                try: results.append(future.result())
                except Exception as exc: failures.append({"session": futures[future], "type": type(exc).__name__, "error": str(exc)})
    finally:
        stop.set(); sampler.join(timeout=2)
    wall_s = time.monotonic() - started
    report = {"mode": a.mode, "inference": a.inference,
              "sessions_requested": len(raw["sessions"]), "sessions_completed": len(results),
              "failures": failures, "wall_s": wall_s,
              "throughput_sessions_per_hour": len(results) * 3600 / wall_s,
              "mean_firecracker_rss_bytes": (
                  sum(x["firecracker_rss_bytes"] for x in samples) / len(samples) if samples else 0
              ),
              "peak_firecracker_rss_bytes": max((x["firecracker_rss_bytes"] for x in samples), default=0),
              "sessions": results}
    (a.output / "memory.json").write_text(json.dumps(samples, indent=2) + "\n")
    (a.output / "summary.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__": main()
