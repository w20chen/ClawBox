from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any

from .engine import JsonlEventWriter, ReplayEngine, SnapshotPolicy
from .latency import LinearLatencyPredictor
from .lifecycle import (
    FirecrackerConfig,
    FirecrackerLifecycle,
    LocalCommandExecutor,
    KubectlCommandExecutor,
    ResidentSlotLifecycle,
    SSHCommandExecutor,
    SimulatedLifecycle,
)
from .trace import load_trace


def _predictor(paths: list[Path]) -> LinearLatencyPredictor:
    actions = [action for path in paths for action in load_trace(path)]
    return LinearLatencyPredictor.from_actions(actions)


def _policy(args: argparse.Namespace) -> SnapshotPolicy:
    return SnapshotPolicy(
        min_predicted_llm_s=args.snapshot_threshold_s,
        estimated_snapshot_s=args.estimated_snapshot_s,
        estimated_restore_s=args.estimated_restore_s,
        safety_margin_s=args.safety_margin_s,
    )


def inspect_trace(args: argparse.Namespace) -> int:
    actions = load_trace(args.trace)
    predictor = _predictor(args.calibration)
    llms = [action for action in actions if action.kind == "llm"]
    tools = [action for action in actions if action.kind == "tool"]
    payload = {
        "trace": str(args.trace),
        "actions": len(actions),
        "llm_actions": len(llms),
        "tool_actions": len(tools),
        "recorded_llm_s": sum(action.duration_s for action in llms),
        "recorded_tool_s": sum(action.duration_s for action in tools),
        "replayable_shell_tools": sum(_is_shell_replayable(action) for action in tools),
        "predictor": predictor.to_dict(),
        "predictions": [
            {
                "action_id": action.action_id,
                "actual_s": action.duration_s,
                "predicted_s": predictor.predict(action),
                "input_chars": action.input_chars,
            }
            for action in llms
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _is_shell_replayable(action: Any) -> bool:
    try:
        action.shell_command()
        return True
    except ValueError:
        return False


def run_replay(args: argparse.Namespace) -> int:
    actions = load_trace(args.trace)
    predictor = _predictor(args.calibration)
    if args.backend == "firecracker":
        if args.firecracker_config is None:
            raise ValueError("firecracker backend requires --firecracker-config")
        lifecycle = FirecrackerLifecycle(FirecrackerConfig.from_json(args.firecracker_config))
        if args.tool_transport == "local":
            executor = LocalCommandExecutor(cwd=args.cwd, workspace_alias="/workspace")
        elif args.tool_transport == "kubectl":
            if not args.tool_pod:
                raise ValueError("kubectl tool transport requires --tool-pod")
            executor = KubectlCommandExecutor(
                namespace=args.tool_namespace, pod=args.tool_pod,
                container=args.tool_container,
            )
        else:
            if args.ssh_identity is None:
                raise ValueError("ssh tool transport requires --ssh-identity")
            executor = SSHCommandExecutor(
                host=args.ssh_host, port=args.ssh_port, user=args.ssh_user,
                identity_file=args.ssh_identity,
            )
    else:
        lifecycle = SimulatedLifecycle(
            snapshot_s=args.estimated_snapshot_s,
            restore_s=args.estimated_restore_s,
        )
        executor = LocalCommandExecutor(cwd=args.cwd)
    writer = JsonlEventWriter(args.events) if args.events else None
    try:
        engine = ReplayEngine(
            lifecycle=lifecycle,
            executor=executor,
            predictor=predictor,
            policy=_policy(args),
            mode=args.mode,
            sleep_scale=args.sleep_scale,
            tool_time_scale=args.tool_time_scale,
            command_timeout_s=args.command_timeout_s,
            strict_exit_codes=not args.allow_exit_mismatch,
            event_sink=writer,
        )
        summary = engine.run(actions)
    finally:
        if writer:
            writer.close()
    print(json.dumps({"summary": asdict(summary), "predictor": predictor.to_dict()}, indent=2, sort_keys=True))
    return 0


def run_experiment(args: argparse.Namespace) -> int:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    sessions = manifest.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("experiment manifest requires a non-empty sessions array")
    slots = BoundedSemaphore(args.resident_slots)
    lifecycles: list[ResidentSlotLifecycle] = []
    lifecycle_lock = threading.Lock()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    stop_sample = threading.Event()
    rss_samples: list[dict[str, Any]] = []

    def sample_rss() -> None:
        while not stop_sample.wait(0.1):
            with lifecycle_lock:
                rss = sum(item.rss_bytes() for item in lifecycles)
                resident = sum(item.resident for item in lifecycles)
            rss_samples.append({
                "elapsed_s": time.monotonic() - wall_start,
                "rss_bytes": rss,
                "resident_vms": resident,
                "numa_memory_used_bytes": _numa_memory_used_bytes(args.numa_node),
            })

    def run_one(index: int, spec: dict[str, Any]) -> dict[str, Any]:
        trace = Path(spec["trace"])
        calibration = [Path(path) for path in spec.get("calibration", [])]
        predictor = _predictor(calibration)
        inner = FirecrackerLifecycle(FirecrackerConfig.from_json(Path(spec["firecracker_config"])))
        lifecycle = ResidentSlotLifecycle(inner, slots)
        with lifecycle_lock:
            lifecycles.append(lifecycle)
        tool_transport = spec.get("tool_transport", "kubectl")
        if tool_transport == "local":
            executor = LocalCommandExecutor(
                cwd=Path(spec["cwd"]), workspace_alias="/workspace",
            )
        elif tool_transport == "kubectl":
            executor = KubectlCommandExecutor(
                namespace=str(spec.get("tool_namespace", "clawbox-benchmarks")),
                pod=str(spec["tool_pod"]),
                container=spec.get("tool_container"),
            )
        elif tool_transport == "ssh":
            executor = SSHCommandExecutor(
                host=str(spec.get("ssh_host", "127.0.0.1")),
                port=int(spec["ssh_port"]),
                user=str(spec.get("ssh_user", "root")),
                identity_file=Path(spec["ssh_identity"]),
            )
        else:
            raise ValueError(f"unsupported tool_transport: {tool_transport!r}")
        writer = JsonlEventWriter(output_dir / f"session-{index:04d}.jsonl")
        try:
            summary = ReplayEngine(
                lifecycle=lifecycle, executor=executor, predictor=predictor,
                policy=_policy(args), mode=args.mode, sleep_scale=args.sleep_scale,
                tool_time_scale=args.tool_time_scale,
                command_timeout_s=args.command_timeout_s,
                strict_exit_codes=not args.allow_exit_mismatch,
                event_sink=writer,
            ).run(load_trace(trace))
            return asdict(summary)
        finally:
            writer.close()

    wall_start = time.monotonic()
    sampler = threading.Thread(target=sample_rss, name="firecracker-rss-sampler", daemon=True)
    sampler.start()
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
            futures = {pool.submit(run_one, index, spec): index for index, spec in enumerate(sessions)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    failures.append({"session": index, "type": type(exc).__name__, "error": str(exc)})
    finally:
        stop_sample.set()
        sampler.join(timeout=2)
    wall_s = time.monotonic() - wall_start
    report = {
        "mode": args.mode,
        "sessions_requested": len(sessions),
        "sessions_completed": len(results),
        "failures": failures,
        "resident_slots": args.resident_slots,
        "wall_s": wall_s,
        "throughput_sessions_per_hour": len(results) / wall_s * 3600 if wall_s else 0.0,
        "peak_firecracker_rss_bytes": max((sample["rss_bytes"] for sample in rss_samples), default=0),
        "peak_numa_memory_used_bytes": max(
            (sample["numa_memory_used_bytes"] for sample in rss_samples
             if sample["numa_memory_used_bytes"] is not None), default=None,
        ),
        "peak_resident_vms": max((sample["resident_vms"] for sample in rss_samples), default=0),
        "mean_session_wall_s": statistics.fmean(item["wall_s"] for item in results) if results else None,
        "total_snapshots": sum(item["snapshots"] for item in results),
        "sessions": results,
    }
    (output_dir / "rss.json").write_text(json.dumps(rss_samples, indent=2) + "\n", encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if failures else 0


def _numa_memory_used_bytes(node: int | None) -> int | None:
    if node is None:
        return None
    path = Path(f"/sys/devices/system/node/node{node}/meminfo")
    try:
        values: dict[str, int] = {}
        for line in path.read_text(encoding="ascii").splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[0] == "Node" and fields[1] == str(node):
                values[fields[2].rstrip(":")] = int(fields[3]) * 1024
        if "MemUsed" in values:
            return values["MemUsed"]
        if "MemTotal" in values and "MemFree" in values:
            return values["MemTotal"] - values["MemFree"]
    except (OSError, ValueError):
        return None
    return None


def _common_replay_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=("resident", "snapshot"), required=True)
    parser.add_argument("--sleep-scale", type=float, default=1.0)
    parser.add_argument(
        "--tool-time-scale", type=float, default=0.0,
        help="pad real tool calls to this fraction of their recorded duration",
    )
    parser.add_argument("--snapshot-threshold-s", type=float, default=20.0)
    parser.add_argument("--estimated-snapshot-s", type=float, default=1.0)
    parser.add_argument("--estimated-restore-s", type=float, default=1.0)
    parser.add_argument("--safety-margin-s", type=float, default=2.0)
    parser.add_argument("--command-timeout-s", type=float, default=300.0)
    parser.add_argument("--allow-exit-mismatch", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay agent traces with optional Firecracker eviction")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="validate and summarize a replay trace")
    inspect_parser.add_argument("trace", type=Path)
    inspect_parser.add_argument("--calibration", type=Path, action="append", default=[])
    inspect_parser.set_defaults(func=inspect_trace)

    run_parser = subparsers.add_parser("run", help="run one replay session")
    run_parser.add_argument("trace", type=Path)
    run_parser.add_argument("--calibration", type=Path, action="append", default=[])
    run_parser.add_argument("--backend", choices=("local", "firecracker"), default="local")
    run_parser.add_argument("--firecracker-config", type=Path)
    run_parser.add_argument("--tool-transport", choices=("local", "ssh", "kubectl"), default="ssh")
    run_parser.add_argument("--ssh-host", default="127.0.0.1")
    run_parser.add_argument("--ssh-port", type=int, default=22)
    run_parser.add_argument("--ssh-user", default="root")
    run_parser.add_argument("--ssh-identity", type=Path)
    run_parser.add_argument("--tool-namespace", default="clawbox-benchmarks")
    run_parser.add_argument("--tool-pod")
    run_parser.add_argument("--tool-container")
    run_parser.add_argument("--cwd", type=Path)
    run_parser.add_argument("--events", type=Path)
    _common_replay_args(run_parser)
    run_parser.set_defaults(func=run_replay)

    experiment = subparsers.add_parser("experiment", help="run concurrent direct-Firecracker sessions")
    experiment.add_argument("manifest", type=Path)
    experiment.add_argument("--output-dir", type=Path, required=True)
    experiment.add_argument("--resident-slots", type=int, required=True)
    experiment.add_argument("--numa-node", type=int)
    _common_replay_args(experiment)
    experiment.set_defaults(func=run_experiment)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "sleep_scale", 0) < 0:
        parser.error("--sleep-scale must be non-negative")
    if getattr(args, "tool_time_scale", 0) < 0:
        parser.error("--tool-time-scale must be non-negative")
    if getattr(args, "resident_slots", 1) < 1:
        parser.error("--resident-slots must be positive")
    try:
        raise SystemExit(args.func(args))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
