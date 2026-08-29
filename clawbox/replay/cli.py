from __future__ import annotations

import argparse
import json
import os
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
from .guest import VsockCommandExecutor, VsockRuntimeAgentClient
from .inference import OpenAIInferenceProvider, TraceReplayInferenceProvider
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
        estimated_refault_s=args.estimated_refault_s,
        safety_margin_s=args.safety_margin_s,
    )


def _tool_policy(args: argparse.Namespace) -> SnapshotPolicy:
    return SnapshotPolicy(
        min_predicted_llm_s=args.tool_snapshot_threshold_s,
        estimated_snapshot_s=args.estimated_snapshot_s,
        estimated_restore_s=args.estimated_restore_s,
        estimated_refault_s=args.estimated_refault_s,
        safety_margin_s=args.safety_margin_s,
    )


def _inference(args: argparse.Namespace):
    if args.inference_backend == "replay":
        return TraceReplayInferenceProvider(
            time_scale=args.sleep_scale,
            simulated_gpu_id=args.simulated_gpu_id,
            simulated_kv_bytes=args.simulated_kv_bytes,
        )
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise ValueError(f"real inference requires environment variable {args.api_key_env}")
    if not args.api_base_url or not args.api_model:
        raise ValueError("real inference requires --api-base-url and --api-model")
    return OpenAIInferenceProvider(
        base_url=args.api_base_url,
        api_key=api_key,
        model=args.api_model,
        timeout_s=args.api_timeout_s,
        trust_env=args.api_trust_env,
    )


def _runtime_agent(config: FirecrackerConfig) -> VsockRuntimeAgentClient | None:
    if config.guest_agent_port is None:
        return None
    if config.vsock_uds is None:
        raise ValueError("guest_agent_port requires vsock_uds in Firecracker config")
    return VsockRuntimeAgentClient(config.vsock_uds, port=config.guest_agent_port)


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
        config = FirecrackerConfig.from_json(args.firecracker_config)
        lifecycle = FirecrackerLifecycle(config)
        runtime_agent = _runtime_agent(config)
        tool_lifecycle = None
        tool_runtime_agent = None
        if args.tool_firecracker_config is not None:
            tool_config = FirecrackerConfig.from_json(args.tool_firecracker_config)
            tool_lifecycle = FirecrackerLifecycle(tool_config)
            tool_runtime_agent = _runtime_agent(tool_config)
            if tool_runtime_agent is None:
                raise ValueError("tool Firecracker config requires guest_agent_port and vsock_uds")
            if args.tool_transport != "vsock":
                raise ValueError(
                    "paired Tool Firecracker requires --tool-transport vsock; "
                    "local, SSH, and kubectl transports are placeholder modes"
                )
            executor = VsockCommandExecutor(tool_runtime_agent)
        elif args.tool_transport == "local":
            executor = LocalCommandExecutor(cwd=args.cwd, workspace_alias="/workspace")
        elif args.tool_transport == "kubectl":
            if not args.tool_pod:
                raise ValueError("kubectl tool transport requires --tool-pod")
            executor = KubectlCommandExecutor(
                namespace=args.tool_namespace, pod=args.tool_pod,
                container=args.tool_container,
            )
        elif args.tool_transport == "ssh":
            if args.ssh_identity is None:
                raise ValueError("ssh tool transport requires --ssh-identity")
            executor = SSHCommandExecutor(
                host=args.ssh_host, port=args.ssh_port, user=args.ssh_user,
                identity_file=args.ssh_identity,
            )
        else:
            raise ValueError("--tool-transport vsock requires --tool-firecracker-config")
    else:
        lifecycle = SimulatedLifecycle(
            snapshot_s=args.estimated_snapshot_s,
            restore_s=args.estimated_restore_s,
        )
        executor = LocalCommandExecutor(cwd=args.cwd)
        runtime_agent = None
        tool_lifecycle = None
        tool_runtime_agent = None
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
            inference_provider=_inference(args), runtime_agent=runtime_agent,
            tool_lifecycle=tool_lifecycle,
            tool_runtime_agent=tool_runtime_agent,
            tool_policy=_tool_policy(args),
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
    tool_slots = BoundedSemaphore(args.tool_resident_slots)
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
        config = FirecrackerConfig.from_json(Path(spec["firecracker_config"]))
        inner = FirecrackerLifecycle(config)
        runtime_agent = _runtime_agent(config)
        lifecycle = ResidentSlotLifecycle(inner, slots)
        with lifecycle_lock:
            lifecycles.append(lifecycle)
        tool_lifecycle = None
        tool_runtime_agent = None
        tool_config_path = spec.get("tool_firecracker_config")
        if tool_config_path is not None:
            tool_config = FirecrackerConfig.from_json(Path(tool_config_path))
            tool_inner = FirecrackerLifecycle(tool_config)
            tool_runtime_agent = _runtime_agent(tool_config)
            if tool_runtime_agent is None:
                raise ValueError("tool Firecracker config requires guest_agent_port and vsock_uds")
            tool_lifecycle = ResidentSlotLifecycle(tool_inner, tool_slots)
            with lifecycle_lock:
                lifecycles.append(tool_lifecycle)
        tool_transport = spec.get("tool_transport", "kubectl")
        if tool_config_path is not None:
            if tool_transport != "vsock":
                raise ValueError(
                    "paired Tool Firecracker requires tool_transport: vsock; "
                    "local, SSH, and kubectl transports are placeholder modes"
                )
            assert tool_runtime_agent is not None
            executor = VsockCommandExecutor(tool_runtime_agent)
        elif tool_transport == "local":
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
                inference_provider=_inference(args), runtime_agent=runtime_agent,
                tool_lifecycle=tool_lifecycle,
                tool_runtime_agent=tool_runtime_agent,
                tool_policy=_tool_policy(args),
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
    firecracker_rss = [int(sample["rss_bytes"]) for sample in rss_samples]
    numa_used = [int(sample["numa_memory_used_bytes"]) for sample in rss_samples
                 if sample["numa_memory_used_bytes"] is not None]
    rss_time = sum(
        firecracker_rss[index] * max(
            0.0, float(rss_samples[index]["elapsed_s"])
            - float(rss_samples[index - 1]["elapsed_s"]),
        )
        for index in range(1, len(rss_samples))
    )
    report = {
        "mode": args.mode,
        "inference_backend": args.inference_backend,
        "sessions_requested": len(sessions),
        "sessions_completed": len(results),
        "failures": failures,
        "resident_slots": args.resident_slots,
        "tool_resident_slots": args.tool_resident_slots,
        "wall_s": wall_s,
        "throughput_sessions_per_hour": len(results) / wall_s * 3600 if wall_s else 0.0,
        "peak_firecracker_rss_bytes": max((sample["rss_bytes"] for sample in rss_samples), default=0),
        "mean_firecracker_rss_bytes": statistics.fmean(firecracker_rss) if firecracker_rss else 0,
        "p95_firecracker_rss_bytes": _percentile(firecracker_rss, 0.95),
        "firecracker_rss_time_byte_seconds": rss_time,
        "mean_numa_memory_used_bytes": statistics.fmean(numa_used) if numa_used else None,
        "peak_numa_memory_used_bytes": max(
            (sample["numa_memory_used_bytes"] for sample in rss_samples
             if sample["numa_memory_used_bytes"] is not None), default=None,
        ),
        "peak_resident_vms": max((sample["resident_vms"] for sample in rss_samples), default=0),
        "mean_session_wall_s": statistics.fmean(item["wall_s"] for item in results) if results else None,
        "total_snapshots": sum(item["snapshots"] for item in results),
        "total_tool_snapshots": sum(item["tool_snapshots"] for item in results),
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


def _percentile(values: list[int], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))])


def _common_replay_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=("resident", "snapshot"), required=True)
    parser.add_argument("--inference-backend", choices=("replay", "api"), default="replay")
    parser.add_argument("--api-base-url")
    parser.add_argument("--api-model")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--api-timeout-s", type=float, default=600.0)
    parser.add_argument("--api-trust-env", action="store_true")
    parser.add_argument("--sleep-scale", type=float, default=1.0)
    parser.add_argument(
        "--tool-time-scale", type=float, default=0.0,
        help="pad real tool calls to this fraction of their recorded duration",
    )
    parser.add_argument("--simulated-gpu-id", default="replay-gpu-unbounded")
    parser.add_argument("--simulated-kv-bytes", type=int, default=0)
    parser.add_argument("--snapshot-threshold-s", type=float, default=20.0)
    parser.add_argument("--tool-snapshot-threshold-s", type=float, default=30.0)
    parser.add_argument("--estimated-snapshot-s", type=float, default=1.0)
    parser.add_argument("--estimated-restore-s", type=float, default=1.0)
    parser.add_argument("--estimated-refault-s", type=float, default=0.0)
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
    run_parser.add_argument("--tool-firecracker-config", type=Path)
    run_parser.add_argument("--tool-transport", choices=("local", "ssh", "kubectl", "vsock"), default="ssh")
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
    experiment.add_argument(
        "--tool-resident-slots", type=int,
        help="Tool VM resident budget; defaults to --resident-slots",
    )
    experiment.add_argument("--numa-node", type=int)
    _common_replay_args(experiment)
    experiment.set_defaults(func=run_experiment)
    study = subparsers.add_parser("study", help="run a reproducible inference x memory-policy matrix")
    study.add_argument("config", type=Path)
    study.set_defaults(func=lambda args: __import__(
        "clawbox.replay.study", fromlist=["run_study"]
    ).run_study(args.config))
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "sleep_scale", 0) < 0:
        parser.error("--sleep-scale must be non-negative")
    if getattr(args, "tool_time_scale", 0) < 0:
        parser.error("--tool-time-scale must be non-negative")
    if getattr(args, "simulated_kv_bytes", 0) < 0:
        parser.error("--simulated-kv-bytes must be non-negative")
    for field in (
        "snapshot_threshold_s", "tool_snapshot_threshold_s",
        "estimated_snapshot_s", "estimated_restore_s", "estimated_refault_s",
        "safety_margin_s",
    ):
        if getattr(args, field, 0) < 0:
            parser.error(f"--{field.replace('_', '-')} must be non-negative")
    if getattr(args, "resident_slots", 1) < 1:
        parser.error("--resident-slots must be positive")
    if getattr(args, "tool_resident_slots", None) is None:
        args.tool_resident_slots = getattr(args, "resident_slots", 1)
    if args.tool_resident_slots < 1:
        parser.error("--tool-resident-slots must be positive")
    try:
        raise SystemExit(args.func(args))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
