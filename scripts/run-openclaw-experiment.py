#!/usr/bin/env python3
"""Run OpenClaw+ClawTune in the Runtime VM with SSH tools and one model gateway."""
from __future__ import annotations
import argparse
from dataclasses import replace
import hashlib
import json
import math
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clawbox.replay.lifecycle import (
    FairResourcePool, FeedbackMemoryAdmission, FirecrackerConfig, FirecrackerLifecycle,
)
from clawbox.replay.model_gateway import ModelGateway


def numa_memory_used_bytes(node: int) -> int | None:
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


def cgroup_v2_path() -> Path | None:
    try:
        for line in Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines():
            fields = line.split(":", 2)
            if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
                return Path("/sys/fs/cgroup") / fields[2].lstrip("/")
    except OSError:
        return None
    return None


def cgroup_memory_used_bytes() -> int | None:
    try:
        path = cgroup_v2_path()
        return None if path is None else int((path / "memory.current").read_text().strip())
    except (OSError, ValueError):
        return None


def percentile(values: list[float | int], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))])


def wait_tcp(host: str, port: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5): return
        except OSError: time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {host}:{port}")


def checkpoint_runtime_tool_pair(
    runtime: FirecrackerLifecycle, tool: FirecrackerLifecycle,
) -> float:
    """Quiesce the actor before its Tool dependency.

    The Runtime may receive the model response at any point after the gateway
    observes a pending request.  Pausing Tool first leaves a race in which a
    still-running Runtime dispatches commands to an unavailable Tool VM.  The
    dependency-safe order is therefore Runtime first, then Tool.
    """
    elapsed = runtime.checkpoint_and_evict()
    elapsed += tool.checkpoint_and_evict()
    return elapsed


def restore_tool_runtime_pair(
    tool: FirecrackerLifecycle,
    runtime: FirecrackerLifecycle,
    wait_tool_ready,
) -> float:
    """Restore the dependency before resuming the actor."""
    elapsed = tool.restore()
    wait_tool_ready()
    elapsed += runtime.restore()
    return elapsed


def close_runtime_tool_pair_and_release(
    runtime: FirecrackerLifecycle,
    tool: FirecrackerLifecycle,
    release_pair,
) -> None:
    """Close both VMs before returning their atomic pair lease.

    In particular, a Tool checkpoint failure can happen after Runtime has
    already been evicted.  Cleanup must still stop the Tool VM and make the
    pair lease reusable; the failed session itself remains failed.
    """
    try:
        runtime.close()
    finally:
        try:
            tool.close()
        finally:
            release_pair()


def complete(log: Path) -> tuple[bool, int | None]:
    if not log.exists(): return False, None
    for line in reversed(log.read_text(errors="replace").splitlines()):
        if line.startswith('{"ok":') and "openclaw_exit_code" in line:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                # The serial-log writer may still be appending its final line.
                # Treat a torn tail as incomplete and poll again.
                return False, None
            return True, int(value["openclaw_exit_code"])
    return False, None


def request_summaries(gateway: ModelGateway) -> list[dict]:
    summaries = []
    for item in gateway.records():
        encoded = item.pop("response_b64", "")
        item.pop("request_payload", None)
        item["response_bytes"] = (len(encoded) * 3 // 4 - encoded.count("=")) if encoded else 0
        summaries.append(item)
    return summaries


def tool_call_descriptors(message: dict) -> list[dict]:
    descriptors = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        command = arguments.get("command") if isinstance(arguments, dict) else None
        descriptors.append({
            "tool_name": str(function.get("name") or ""),
            "command_sha256": (
                hashlib.sha256(command.encode()).hexdigest()
                if isinstance(command, str) else None
            ),
        })
    return descriptors


def peak_overlapping_memory_bytes(executions: list[dict]) -> int:
    """Conservative peak for commands attributed to one admitted Tool batch."""
    if not executions:
        return 0
    points = sorted({
        float(value)
        for item in executions
        for value in (item["ts_start"], item["ts_end"])
    })
    return max(
        sum(
            int(item["actual_command_peak_memory_bytes"])
            for item in executions
            if float(item["ts_start"]) <= point <= float(item["ts_end"])
        )
        for point in points
    )


def collect_tool_working_sets(
    spec: dict, events: list[dict], output: Path,
    *, arm_started_unix_s: float | None = None,
) -> list[dict]:
    command = (
        "cd /testbed && { printf '__CLAWBOX_BRIDGE__\\n'; "
        "cat .clawbox/tool-bridge.jsonl 2>/dev/null || true; "
        "printf '__CLAWBOX_CGROUP__\\n'; "
        "find .clawbox/tool-resource -type f -name 'cgroup-resource-*.json' "
        "-exec cat {} \\; 2>/dev/null || true; }"
    )
    exit_code, stdout, stderr, timed_out = ssh_capture(spec, command, 30)
    output.write_bytes(stdout + b"\n__CLAWBOX_STDERR__\n" + stderr)
    if exit_code != 0 or timed_out:
        raise RuntimeError("failed to collect Tool working-set artifacts")
    text = stdout.decode(errors="replace")
    before, separator, after = text.partition("__CLAWBOX_CGROUP__\n")
    if not separator:
        raise RuntimeError("Tool working-set output is missing its cgroup marker")
    bridge_text = before.partition("__CLAWBOX_BRIDGE__\n")[2]
    bridges = [json.loads(line) for line in bridge_text.splitlines() if line.startswith("{")]
    cgroups = [json.loads(line) for line in after.splitlines() if line.startswith("{")]
    by_execution = {
        str(item.get("execution_id")): item for item in cgroups
        if item.get("execution_id") and item.get("memory_rss_peak_bytes") is not None
        and item.get("sampling_quality") == "valid"
    }
    actual = []
    for bridge in bridges:
        resource = by_execution.get(str(bridge.get("execution_id")))
        if resource is None:
            continue
        actual.append({
            "command_sha256": bridge.get("command_sha256"),
            "execution_id": bridge.get("execution_id"),
            "execution_source": bridge.get("execution_source"),
            "actual_command_peak_memory_bytes": int(resource["memory_rss_peak_bytes"]),
            "ts_start": float(resource.get("ts_start", 0.0)),
            "ts_end": float(resource.get("ts_end", resource.get("ts_start", 0.0))),
            "memory_source": resource.get("memory_source"),
            "monitor_source": resource.get("monitor_source"),
            "fallback_used": bool(resource.get("fallback_used")),
        })
    used: set[int] = set()
    joined = []
    for event in events:
        matches: list[tuple[int, dict]] = []
        join_method = "command_sha256"
        acquired = event.get("acquired_elapsed_s")
        released = event.get("released_elapsed_s")
        if arm_started_unix_s is not None and acquired is not None and released is not None:
            window_start = arm_started_unix_s + float(acquired)
            window_end = arm_started_unix_s + float(released)
            matches = [
                (index, item) for index, item in enumerate(actual)
                if index not in used
                and item.get("execution_source") == "runtime-envelope"
                and window_start <= float(item["ts_start"]) <= window_end
            ]
            join_method = "admission_time_window"
        if not matches:
            digests = {
                invocation.get("command_sha256")
                for invocation in event.get("tool_invocations") or []
                if invocation.get("command_sha256")
            }
            matches = [
                (index, item) for index, item in enumerate(actual)
                if index not in used and item.get("command_sha256") in digests
            ]
            join_method = "command_sha256"
        if not matches:
            continue
        used.update(index for index, _item in matches)
        executions = [item for _index, item in matches]
        actual_peak = peak_overlapping_memory_bytes(executions)
        actual_mib = actual_peak / (1024.0 * 1024.0)
        predicted = event.get("predicted_incremental_p90_mib")
        if predicted is None and join_method == "command_sha256":
            invocation_predictions = [
                invocation.get("predicted_command_memory_p90_mib")
                for invocation in event.get("tool_invocations") or []
                if invocation.get("predicted_command_memory_p90_mib") is not None
            ]
            if len(invocation_predictions) == 1:
                predicted = invocation_predictions[0]
        if predicted is None:
            predicted = event.get("reservation_mib")
        row = {
            "model_step": event.get("model_step"),
            "reservation_mib": event.get("reservation_mib"),
            "predicted_command_memory_p90_mib": predicted,
            "actual_command_peak_memory_bytes": actual_peak,
            "actual_individual_peak_memory_bytes": max(
                int(item["actual_command_peak_memory_bytes"]) for item in executions
            ),
            "actual_execution_count": len(executions),
            "execution_ids": [item["execution_id"] for item in executions],
            "command_sha256s": [item["command_sha256"] for item in executions],
            "join_method": join_method,
            "memory_sources": sorted({str(item["memory_source"]) for item in executions}),
            "monitor_sources": sorted({str(item["monitor_source"]) for item in executions}),
            "fallback_used": any(item["fallback_used"] for item in executions),
        }
        if predicted is not None:
            row["prediction_error_mib"] = float(predicted) - actual_mib
            row["prediction_covered_actual"] = float(predicted) >= actual_mib
        joined.append(row)
    return joined


def predictive_steps_from_plan(plan_payload: dict, workload: str) -> dict[int, dict]:
    command_headroom = float(
        plan_payload["per_tool_memory"].get("command_headroom_fraction", 0.0)
    )
    workload_plan = plan_payload["per_tool_memory"]["workloads"][workload]
    steps: dict[int, dict] = {}
    for invocation in workload_plan["tool_invocations"]:
        step = int(invocation["model_step"])
        current = steps.setdefault(step, {
            "incremental_p90_kib": 0, "tool_invocations": [],
        })
        incremental_kib = int(invocation.get("incremental_p90_kib") or math.ceil(
            float(invocation["predicted_command_memory_p90_mib"])
            * (1.0 + command_headroom) * 1024.0
        ))
        # The gateway releases a response containing the whole Tool batch. It
        # cannot gate individual calls inside the guest, so concurrent demand
        # is conservatively additive rather than the maximum single call.
        current["incremental_p90_kib"] += incremental_kib
        current["tool_invocations"].append(invocation)
    return steps


def ssh_capture(
    spec: dict, command: str, timeout_s: float,
) -> tuple[int, bytes, bytes, bool]:
    marker = b"\n__CLAWBOX_VALIDATION_EXIT__:"
    remote = f"{command}; status=$?; printf '\\n__CLAWBOX_VALIDATION_EXIT__:%d\\n' \"$status\""
    process = subprocess.Popen([
        "ssh", "-p", "2222", "-i", spec["identity"], "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={spec['known_hosts']}",
        f"executor@{spec['tool_host']}", remote,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        timed_out = True
    before, found, after = stdout.rpartition(marker)
    if not found:
        if timed_out:
            return 124, stdout, stderr, True
        raise RuntimeError(
            f"remote command did not return a completion marker: {stderr.decode(errors='replace')}"
        )
    exit_text = after.splitlines()[0]
    try:
        exit_code = int(exit_text)
    except ValueError as exc:
        raise RuntimeError("remote command returned a malformed exit marker") from exc
    return exit_code, before, stderr, timed_out


def ssh_validate(spec: dict, command: str) -> bytes:
    exit_code, stdout, stderr, _ = ssh_capture(spec, command, 15)
    if exit_code != 0:
        raise RuntimeError(
            f"validation command exited with {exit_code}: {stderr.decode(errors='replace')}"
        )
    return stdout


def adjust_balloon(
    tool: FirecrackerLifecycle, target_mib: int, reason: str, timeout_s: float,
) -> dict:
    """Adjust a live Tool balloon and record host RSS plus guest-cooperative progress."""
    started = time.monotonic()
    rss_before = tool.rss_bytes()
    stats = tool.set_balloon_target_mib(target_mib)
    reached = False
    deadline = started + timeout_s
    while True:
        actual = stats.get("actual_mib") if isinstance(stats, dict) else None
        if actual is not None:
            actual = int(actual)
            reached = actual <= target_mib if target_mib == 0 else actual >= target_mib
        if reached or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
        stats = tool.balloon_statistics()
    rss_after = tool.rss_bytes()
    return {
        "reason": reason,
        "target_mib": target_mib,
        "target_reached": reached,
        "statistics": stats,
        "operation_s": time.monotonic() - started,
        "tool_firecracker_rss_before_bytes": rss_before,
        "tool_firecracker_rss_after_bytes": rss_after,
        "tool_firecracker_rss_released_bytes": max(0, rss_before - rss_after),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("manifest", type=Path)
    p.add_argument("--output", required=True, type=Path)
    residency = p.add_mutually_exclusive_group(required=True)
    residency.add_argument("--residency-policy", choices=("resident", "llm_wait_checkpoint"))
    residency.add_argument("--mode", choices=("resident", "snapshot"),
                           help="legacy alias; snapshot means llm_wait_checkpoint")
    p.add_argument("--inference", choices=("replay", "api"), required=True)
    p.add_argument("--trace", type=Path)
    p.add_argument("--time-scale", type=float, default=1.0)
    p.add_argument("--api-base-url"); p.add_argument("--api-model")
    p.add_argument("--api-key-env", default="OPENAI_API_KEY")
    p.add_argument("--validation-command", default="cd /testbed && git diff --binary --no-ext-diff HEAD")
    p.add_argument("--correctness-command")
    p.add_argument("--correctness-timeout-s", type=float, default=300)
    p.add_argument("--timeout-s", type=float, default=900)
    p.add_argument(
        "--resident-memory-budget-mib", type=int,
        help="configured Runtime+Tool resident-memory admission budget",
    )
    p.add_argument("--tool-reservation-budget-mib", type=int)
    p.add_argument("--tool-admission-safety-headroom-mib", type=int, default=1024)
    p.add_argument("--idle-tool-vm-rss-mib", type=float)
    p.add_argument("--static-tool-reservation-mib", type=int)
    p.add_argument("--tool-memory-plan", type=Path)
    p.add_argument("--tool-memory-workload")
    p.add_argument("--tool-balloon-reclamation", action="store_true")
    p.add_argument("--tool-balloon-idle-floor-mib", type=int)
    p.add_argument("--tool-balloon-settle-timeout-s", type=float, default=5.0)
    a = p.parse_args()
    if a.timeout_s <= 0:
        p.error("--timeout-s must be positive")
    if a.correctness_timeout_s <= 0:
        p.error("--correctness-timeout-s must be positive")
    if a.time_scale < 0:
        p.error("--time-scale must be non-negative")
    if a.resident_memory_budget_mib is not None and a.resident_memory_budget_mib <= 0:
        p.error("--resident-memory-budget-mib must be positive")
    if a.tool_reservation_budget_mib is not None and a.tool_reservation_budget_mib <= 0:
        p.error("--tool-reservation-budget-mib must be positive")
    if a.tool_reservation_budget_mib is not None:
        if (a.static_tool_reservation_mib is None) == (a.tool_memory_plan is None):
            p.error("reservation budget requires exactly one static reservation or tool plan")
        if a.idle_tool_vm_rss_mib is None or a.idle_tool_vm_rss_mib <= 0:
            p.error("reservation budget requires positive --idle-tool-vm-rss-mib")
        if (a.static_tool_reservation_mib is not None
                and a.static_tool_reservation_mib > a.tool_reservation_budget_mib):
            p.error("static Tool reservation exceeds the reservation budget")
        if (a.tool_admission_safety_headroom_mib < 0
                or a.tool_admission_safety_headroom_mib >= a.tool_reservation_budget_mib):
            p.error("Tool admission safety headroom must be non-negative and below budget")
    if a.tool_memory_plan is not None and not a.tool_memory_workload:
        p.error("--tool-memory-workload is required with --tool-memory-plan")
    if a.inference == "replay" and a.trace is None:
        p.error("--trace is required when --inference=replay")
    if a.inference == "api" and (not a.api_base_url or not a.api_model):
        p.error("--api-base-url and --api-model are required when --inference=api")
    if a.inference == "api" and not os.environ.get(a.api_key_env):
        p.error(f"environment variable {a.api_key_env!r} is required when --inference=api")
    a.residency_policy = a.residency_policy or (
        "llm_wait_checkpoint" if a.mode == "snapshot" else a.mode
    )
    if a.tool_balloon_reclamation:
        if a.residency_policy != "resident":
            p.error("Tool balloon reclamation is a resident-only baseline")
        if a.tool_memory_plan is None:
            p.error("Tool balloon reclamation requires a per-tool memory plan")
        if (a.tool_balloon_idle_floor_mib is None
                or a.tool_balloon_idle_floor_mib <= 0):
            p.error("Tool balloon reclamation requires a positive idle floor")
        if a.tool_balloon_settle_timeout_s <= 0:
            p.error("Tool balloon settle timeout must be positive")
    raw = json.loads(a.manifest.read_text())
    configured_nodes = {
        FirecrackerConfig.from_json(Path(session[field])).numa_node
        for session in raw["sessions"] for field in ("runtime", "tool")
    }
    if len(configured_nodes) != 1 or None in configured_nodes:
        raise ValueError("all Runtime and Tool VMs must use one explicit NUMA node")
    numa_node = int(next(iter(configured_nodes)))
    memory_splits = {
        (
            FirecrackerConfig.from_json(Path(session["runtime"])).memory_mib,
            FirecrackerConfig.from_json(Path(session["tool"])).memory_mib,
        )
        for session in raw["sessions"]
    }
    if len(memory_splits) != 1:
        raise ValueError("all sessions must use the same Runtime/Tool memory split")
    runtime_memory_mib, tool_memory_mib = next(iter(memory_splits))
    if (a.tool_balloon_reclamation
            and a.tool_balloon_idle_floor_mib >= tool_memory_mib):
        p.error("Tool balloon idle floor must be below fixed Tool-VM capacity")
    pair_memory_mib = int(runtime_memory_mib + tool_memory_mib)
    resident_pair_slots = len(raw["sessions"])
    if a.resident_memory_budget_mib is not None:
        resident_pair_slots = a.resident_memory_budget_mib // pair_memory_mib
        if resident_pair_slots < 1:
            raise ValueError(
                "resident-memory budget is smaller than one Runtime+Tool VM pair"
            )
        resident_pair_slots = min(resident_pair_slots, len(raw["sessions"]))
    configured_cpu_pairs = raw.get("cpu_pairs")
    if configured_cpu_pairs is not None:
        if not isinstance(configured_cpu_pairs, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("runtime"), int)
            or not isinstance(item.get("tool"), int)
            for item in configured_cpu_pairs
        ):
            raise ValueError("manifest cpu_pairs must contain integer Runtime/Tool pairs")
        cpu_pairs = [
            (int(item["runtime"]), int(item["tool"]))
            for item in configured_cpu_pairs
        ]
        if resident_pair_slots > len(cpu_pairs):
            raise ValueError("resident pair budget exceeds the available CPU-pair leases")
        pair_resources: list[object] = list(cpu_pairs[:resident_pair_slots])
    else:
        pair_resources = list(range(resident_pair_slots))
    pair_slots = FairResourcePool(pair_resources)
    predictive_steps: dict[int, dict] = {}
    if a.tool_memory_plan is not None:
        plan_payload = json.loads(a.tool_memory_plan.read_text(encoding="utf-8"))
        predictive_steps = predictive_steps_from_plan(
            plan_payload, a.tool_memory_workload
        )
    a.output.mkdir(parents=True, exist_ok=False)
    lifecycles: list[FirecrackerLifecycle] = []
    tool_lifecycles: list[FirecrackerLifecycle] = []
    lifecycle_lock = threading.Lock()
    def measure_tool_resident_bytes() -> int:
        with lifecycle_lock:
            return sum(item.rss_bytes() for item in tool_lifecycles)
    tool_admission = (
        FeedbackMemoryAdmission(
            a.tool_reservation_budget_mib * 1024 * 1024,
            a.tool_admission_safety_headroom_mib * 1024 * 1024,
            measure_tool_resident_bytes,
        )
        if a.tool_reservation_budget_mib is not None else None
    )
    samples: list[dict] = []
    stop = threading.Event()
    started_unix_s = time.time()
    started = time.monotonic()
    cgroup_baseline = cgroup_memory_used_bytes()
    cgroup_path = cgroup_v2_path()
    numa_baseline = numa_memory_used_bytes(numa_node)
    def sample() -> None:
        while not stop.wait(0.1):
            with lifecycle_lock:
                rss = sum(item.rss_bytes() for item in lifecycles)
                tool_rss = sum(item.rss_bytes() for item in tool_lifecycles)
                resident_vms = sum(1 for item in lifecycles if item.resident)
            admission_status = (
                tool_admission.observe(tool_rss) if tool_admission is not None else None
            )
            cgroup_memory = cgroup_memory_used_bytes()
            numa_memory = numa_memory_used_bytes(numa_node)
            samples.append({
                "elapsed_s": time.monotonic() - started,
                "firecracker_rss_bytes": rss,
                "tool_firecracker_rss_bytes": tool_rss,
                "resident_vms": resident_vms,
                "numa_memory_used_bytes": numa_memory,
                "numa_memory_delta_bytes": (
                    None if numa_baseline is None or numa_memory is None
                    else max(0, numa_memory - numa_baseline)
                ),
                "cgroup_memory_used_bytes": cgroup_memory,
                "cgroup_memory_delta_bytes": (
                    None if cgroup_memory is None or cgroup_baseline is None
                    else max(0, cgroup_memory - cgroup_baseline)
                ),
                "tool_admission": admission_status,
                "tool_resident_plus_headroom_over_budget": (
                    admission_status is not None
                    and tool_rss + admission_status["safety_headroom_bytes"]
                    > admission_status["budget_bytes"]
                ),
            })
    sampler = threading.Thread(target=sample, daemon=True); sampler.start()

    def run_one(index: int, spec: dict) -> dict:
        session_started_elapsed_s = time.monotonic() - started
        tool_config, runtime_config = FirecrackerConfig.from_json(Path(spec["tool"])), FirecrackerConfig.from_json(Path(spec["runtime"]))
        remaining = lambda: max(0.0, deadline - time.monotonic())
        tool = FirecrackerLifecycle(tool_config)
        runtime = FirecrackerLifecycle(runtime_config)
        if a.tool_balloon_reclamation:
            tool.config = replace(tool.config, balloon_enabled=True)
        with lifecycle_lock:
            lifecycles.extend([tool, runtime])
            tool_lifecycles.append(tool)
        snapshots = 0; snapshot_s = restore_s = 0.0
        admission_wait_s = 0.0
        admission_acquisitions = 0
        admission_wait_events_s: list[float] = []
        admission_leases: list[dict] = []
        pair_lease: object | None = None
        current_lease_event: dict | None = None
        tool_reservation_lease: int | None = None
        tool_reservation_events: list[dict] = []
        checkpoint_reclamation_events: list[dict] = []
        balloon_events: list[dict] = []
        tool_materialization_events: list[dict] = []
        current_tool_reservation_event: dict | None = None
        tool_reservation_lock = threading.Lock()
        session_closing = threading.Event()

        def release_tool_reservation() -> None:
            nonlocal tool_reservation_lease, current_tool_reservation_event
            with tool_reservation_lock:
                lease = tool_reservation_lease
                tool_reservation_lease = None
                event = current_tool_reservation_event
                current_tool_reservation_event = None
            if lease is not None:
                assert tool_admission is not None
                if a.tool_balloon_reclamation and tool.resident:
                    balloon_event = adjust_balloon(
                        tool,
                        tool_memory_mib - int(a.tool_balloon_idle_floor_mib),
                        "tool_end_idle_reclaim",
                        a.tool_balloon_settle_timeout_s,
                    )
                    balloon_events.append(balloon_event)
                    if event is not None:
                        event["balloon_reclamation"] = balloon_event
                released_status = tool_admission.release(lease)
                if event is not None:
                    released = time.monotonic() - started
                    event["released_elapsed_s"] = released
                    event["held_s"] = (
                        released - event["acquired_elapsed_s"]
                    )
                    event["resident_tool_rss_after_mib"] = (
                        released_status["resident_bytes"] / (1024.0 * 1024.0)
                    )
                    event["remaining_headroom_after_mib"] = (
                        released_status["remaining_headroom_bytes"] / (1024.0 * 1024.0)
                    )

        def before_response_ready(step: int | None, message: dict) -> dict:
            nonlocal tool_reservation_lease, current_tool_reservation_event
            tool_calls = message.get("tool_calls") or []
            if not tool_calls or tool_admission is None:
                return {}
            if session_closing.is_set():
                raise RuntimeError("session closed before Tool admission")
            if a.static_tool_reservation_mib is not None:
                current_tool_rss_kib = math.ceil(tool.rss_bytes() / 1024.0)
                incremental_kib = max(
                    1,
                    int(a.static_tool_reservation_mib) * 1024 - current_tool_rss_kib,
                )
                provenance = {"policy": "static",
                              "static_capacity_mib": int(a.static_tool_reservation_mib),
                              "session_tool_rss_before_mib": current_tool_rss_kib / 1024.0,
                              "tool_invocations": tool_call_descriptors(message)}
            else:
                if step is None or step not in predictive_steps:
                    raise RuntimeError(f"predictive plan has no tool reservation for model step {step}")
                planned = predictive_steps[step]
                if len(planned["tool_invocations"]) != len(tool_calls):
                    raise RuntimeError(f"predictive plan diverged at model step {step}")
                incremental_kib = int(planned["incremental_p90_kib"])
                provenance = {"policy": "per_tool_incremental_p90", **planned}
            wait_started = time.monotonic()
            acquired = tool_admission.acquire(
                incremental_kib * 1024, timeout=remaining()
            )
            wait_s = time.monotonic() - wait_started
            if acquired is None:
                raise TimeoutError("timed out waiting for live-RSS Tool admission")
            lease, admission_status = acquired
            event = {
                "model_step": step,
                "reservation_kib": incremental_kib,
                "reservation_mib": incremental_kib / 1024.0,
                "predicted_incremental_p90_mib": incremental_kib / 1024.0,
                "resident_tool_rss_before_mib": (
                    admission_status["resident_bytes"] / (1024.0 * 1024.0)
                ),
                "outstanding_incremental_mib": (
                    admission_status["outstanding_incremental_bytes"] / (1024.0 * 1024.0)
                ),
                "admission_charge_mib": (
                    admission_status["admission_charge_bytes"] / (1024.0 * 1024.0)
                ),
                "remaining_headroom_mib": (
                    admission_status["remaining_headroom_bytes"] / (1024.0 * 1024.0)
                ),
                "wait_s": wait_s,
                "acquired_elapsed_s": time.monotonic() - started,
                "released_elapsed_s": None,
                "held_s": None,
                **provenance,
            }
            with tool_reservation_lock:
                if session_closing.is_set():
                    reject = True
                else:
                    if tool_reservation_lease is not None:
                        raise RuntimeError("session already owns a Tool admission")
                    reject = False
                    tool_reservation_lease = lease
                    tool_reservation_events.append(event)
                    current_tool_reservation_event = event
            if not reject and a.tool_balloon_reclamation:
                balloon_event = adjust_balloon(
                    tool, 0, "tool_start_expand", a.tool_balloon_settle_timeout_s,
                )
                balloon_events.append(balloon_event)
                event["balloon_expansion"] = balloon_event
            if reject:
                tool_admission.release(lease)
                raise RuntimeError("session closed while waiting for Tool admission")
            return event

        gateway = ModelGateway(
            Path(spec["store"]), mode=a.inference, trace=a.trace, time_scale=a.time_scale,
            upstream_base_url=a.api_base_url,
            upstream_api_key=os.environ.get(a.api_key_env), upstream_model=a.api_model,
            on_request_started=release_tool_reservation,
            before_response_ready=before_response_ready,
        )

        def acquire_pair() -> None:
            nonlocal admission_wait_s, admission_acquisitions, pair_lease, current_lease_event
            if pair_lease is not None:
                raise RuntimeError("resident VM pair already owns an admission lease")
            wait_started = time.monotonic()
            lease = pair_slots.acquire(timeout=remaining())
            waited_s = time.monotonic() - wait_started
            admission_wait_s += waited_s
            admission_wait_events_s.append(waited_s)
            if lease is None:
                raise TimeoutError("timed out waiting for resident-pair admission")
            pair_lease = lease
            admission_acquisitions += 1
            current_lease_event = {
                "acquired_elapsed_s": time.monotonic() - started,
                "released_elapsed_s": None,
                "wait_s": waited_s,
                "runtime_cpu": lease[0] if isinstance(lease, tuple) else runtime.config.cpu_set,
                "tool_cpu": lease[1] if isinstance(lease, tuple) else tool.config.cpu_set,
            }
            admission_leases.append(current_lease_event)
            if isinstance(lease, tuple):
                runtime_cpu, tool_cpu = lease
                runtime.config = replace(runtime.config, cpu_set=str(runtime_cpu))
                tool.config = replace(tool.config, cpu_set=str(tool_cpu))

        def with_tool_materialization_admission(reason: str, operation):
            """Gate boot/restore RSS materialization against the Tool budget."""
            if tool_admission is None:
                return operation()
            wait_started = time.monotonic()
            acquired = tool_admission.acquire(
                tool_memory_mib * 1024 * 1024, timeout=remaining(),
            )
            wait_s = time.monotonic() - wait_started
            if acquired is None:
                raise TimeoutError(
                    f"timed out waiting for Tool {reason} memory admission"
                )
            lease, admitted = acquired
            event = {
                "reason": reason,
                "reservation_mib": tool_memory_mib,
                "wait_s": wait_s,
                "resident_tool_rss_before_mib": (
                    admitted["resident_bytes"] / (1024.0 * 1024.0)
                ),
                "acquired_elapsed_s": time.monotonic() - started,
                "released_elapsed_s": None,
            }
            tool_materialization_events.append(event)
            try:
                return operation()
            finally:
                released = tool_admission.release(lease)
                event["released_elapsed_s"] = time.monotonic() - started
                event["resident_tool_rss_after_mib"] = (
                    released["resident_bytes"] / (1024.0 * 1024.0)
                )
                event["remaining_headroom_after_mib"] = (
                    released["remaining_headroom_bytes"] / (1024.0 * 1024.0)
                )

        def release_pair() -> None:
            nonlocal pair_lease, current_lease_event
            if pair_lease is None:
                return
            lease, pair_lease = pair_lease, None
            if current_lease_event is not None:
                current_lease_event["released_elapsed_s"] = time.monotonic() - started
                current_lease_event = None
            pair_slots.release(lease)

        gateway.start(spec["gateway_host"], 18081)
        deadline = time.monotonic() + a.timeout_s
        try:
            acquire_pair()
            def start_tool():
                elapsed = tool.start()
                wait_tcp(spec["tool_host"], 2222, 30)
                return elapsed
            with_tool_materialization_admission("initial_boot", start_tool)
            runtime.start()
            processed: set[str] = set()
            while time.monotonic() < deadline:
                finished, exit_code = complete(Path(runtime_config.log_path))
                if finished:
                    if exit_code != 0: raise RuntimeError(f"OpenClaw exited with {exit_code}")
                    gateway_records = gateway.records()
                    failed_gateway_records = [
                        item for item in gateway_records
                        if not item["ready"] or item["error"] or item["status_code"] != 200
                    ]
                    if failed_gateway_records:
                        first = failed_gateway_records[0]
                        raise RuntimeError(
                            "model gateway failed at replay step "
                            f"{first.get('replay_index')}: "
                            f"{first.get('error') or first.get('status_code')}"
                        )
                    if a.inference == "replay" and len(gateway_records) != len(gateway.actions):
                        raise RuntimeError(
                            "OpenClaw completed before exhausting replay trace: "
                            f"{len(gateway_records)}/{len(gateway.actions)} model steps"
                        )
                    if a.inference == "api":
                        gateway.write_replay_trace(
                            a.output / f"model-trace-session-{index:04d}.jsonl"
                        )
                    correctness_exit_code = None
                    correctness_timed_out = False
                    correctness_path = None
                    working_set_path = a.output / f"tool-working-set-session-{index:04d}.out"
                    if a.tool_balloon_reclamation:
                        balloon_events.append(adjust_balloon(
                            tool, 0, "validation_expand",
                            a.tool_balloon_settle_timeout_s,
                        ))
                    tool_working_sets = collect_tool_working_sets(
                        spec, tool_reservation_events, working_set_path,
                        arm_started_unix_s=started_unix_s,
                    )
                    if a.correctness_command:
                        (correctness_exit_code, correctness_stdout,
                         correctness_stderr, correctness_timed_out) = ssh_capture(
                            spec, a.correctness_command, a.correctness_timeout_s,
                        )
                        correctness_path = (
                            a.output / f"correctness-session-{index:04d}.out"
                        )
                        correctness_path.write_bytes(
                            correctness_stdout
                            + b"\n__CLAWBOX_STDERR__\n"
                            + correctness_stderr
                        )
                    validation = ssh_validate(spec, a.validation_command)
                    validation_path = a.output / f"validation-session-{index:04d}.out"
                    validation_path.write_bytes(validation)
                    session_completed_elapsed_s = time.monotonic() - started
                    return {"session": index, "snapshots": snapshots,
                            "checkpoint_cycles": snapshots,
                            "vm_snapshot_operations": snapshots * 2,
                            "vm_restore_operations": snapshots * 2,
                            "admission_wait_s": admission_wait_s,
                            "admission_acquisitions": admission_acquisitions,
                            "admission_wait_events_s": admission_wait_events_s,
                            "admission_leases": admission_leases,
                            "session_started_elapsed_s": session_started_elapsed_s,
                            "session_completed_elapsed_s": session_completed_elapsed_s,
                            "session_wall_s": (
                                session_completed_elapsed_s - session_started_elapsed_s
                            ),
                            "snapshot_allocated_bytes": (
                                tool.snapshot_allocated_bytes()
                                + runtime.snapshot_allocated_bytes()
                            ),
                            "correctness_evaluated": bool(a.correctness_command),
                            "correctness_exit_code": correctness_exit_code,
                            "correctness_timed_out": correctness_timed_out,
                            "correctness_artifact": (
                                str(correctness_path) if correctness_path else None
                            ),
                            "snapshot_s": snapshot_s, "restore_s": restore_s,
                            "tool_reservation_events": tool_reservation_events,
                            "checkpoint_reclamation_events": checkpoint_reclamation_events,
                            "balloon_events": balloon_events,
                            "tool_materialization_events": tool_materialization_events,
                            "tool_working_sets": tool_working_sets,
                            "tool_working_set_artifact": str(working_set_path),
                            "validation_sha256": hashlib.sha256(validation).hexdigest(),
                            "validation_artifact": str(validation_path),
                            "model_requests": request_summaries(gateway)}
                runtime_exit = runtime.process_exit_code()
                tool_exit = tool.process_exit_code()
                if runtime_exit is not None:
                    raise RuntimeError(f"Runtime VM exited unexpectedly ({runtime_exit})")
                if tool_exit is not None:
                    raise RuntimeError(f"Tool VM exited unexpectedly ({tool_exit})")
                pending = [item for item in gateway.records()
                           if not item["ready"] and item["request_id"] not in processed]
                if a.residency_policy == "llm_wait_checkpoint" and pending:
                    request_id = pending[0]["request_id"]
                    pair_rss_before = runtime.rss_bytes() + tool.rss_bytes()
                    cgroup_before = cgroup_memory_used_bytes()
                    numa_before = numa_memory_used_bytes(numa_node)
                    snapshot_s += checkpoint_runtime_tool_pair(runtime, tool)
                    pair_rss_after = runtime.rss_bytes() + tool.rss_bytes()
                    if pair_rss_after != 0:
                        raise RuntimeError(
                            "checkpoint did not release the Runtime/Tool Firecracker RSS"
                        )
                    cgroup_after = cgroup_memory_used_bytes()
                    numa_after = numa_memory_used_bytes(numa_node)
                    checkpoint_reclamation_events.append({
                        "request_id": request_id,
                        "pair_firecracker_rss_before_bytes": pair_rss_before,
                        "pair_firecracker_rss_after_bytes": pair_rss_after,
                        "verified_pair_firecracker_rss_released_bytes": (
                            pair_rss_before - pair_rss_after
                        ),
                        "cgroup_memory_before_bytes": cgroup_before,
                        "cgroup_memory_after_bytes": cgroup_after,
                        "cgroup_memory_released_bytes": (
                            None if cgroup_before is None or cgroup_after is None
                            else max(0, cgroup_before - cgroup_after)
                        ),
                        "numa_memory_before_bytes": numa_before,
                        "numa_memory_after_bytes": numa_after,
                        "numa_memory_released_bytes": (
                            None if numa_before is None or numa_after is None
                            else max(0, numa_before - numa_after)
                        ),
                    })
                    # The memory/CPU-pair lease is released only after both
                    # Firecracker processes have been evicted.
                    release_pair()
                    while time.monotonic() < deadline:
                        current = {item["request_id"]: item for item in gateway.records()}
                        if current[request_id]["ready"]: break
                        time.sleep(0.02)
                    acquire_pair()
                    restore_s += with_tool_materialization_admission(
                        "snapshot_restore",
                        lambda: restore_tool_runtime_pair(
                            tool, runtime,
                            lambda: wait_tcp(spec["tool_host"], 2222, 30),
                        ),
                    )
                    processed.add(request_id); snapshots += 1
                else: time.sleep(0.05)
            raise TimeoutError("OpenClaw experiment timed out")
        finally:
            session_closing.set()
            try:
                close_runtime_tool_pair_and_release(runtime, tool, release_pair)
            finally:
                try:
                    release_tool_reservation()
                finally:
                    gateway.close()

    results, failures = [], []
    try:
        with ThreadPoolExecutor(max_workers=len(raw["sessions"])) as pool:
            futures = {pool.submit(run_one, i, s): i for i, s in enumerate(raw["sessions"])}
            for future in as_completed(futures):
                try: results.append(future.result())
                except Exception as exc: failures.append({"session": futures[future], "type": type(exc).__name__, "error": str(exc)})
    finally:
        stop.set(); sampler.join(timeout=2)
    if tool_admission is not None:
        cleanup_deadline = time.monotonic() + 2.0
        while tool_admission.metrics()["active_leases"] and time.monotonic() < cleanup_deadline:
            tool_admission.observe()
            time.sleep(0.01)
        leaked = tool_admission.metrics()["active_leases"]
        if leaked:
            failures.append({
                "session": None,
                "type": "ToolAdmissionLeak",
                "error": f"{leaked} Tool admission leases remained after session cleanup",
            })
    wall_s = time.monotonic() - started
    model_steps = sum(len(item.get("model_requests", [])) for item in results)
    correct_sessions = sum(
        item.get("correctness_evaluated") is True
        and item.get("correctness_exit_code") == 0
        for item in results
    )
    model_requests = [request for item in results for request in item.get("model_requests", [])]
    validated_requests = sum(request.get("replay_input_match") is not None for request in model_requests)
    rss_values = [int(item["firecracker_rss_bytes"]) for item in samples]
    numa_values = [int(item["numa_memory_used_bytes"]) for item in samples
                   if item["numa_memory_used_bytes"] is not None]
    numa_deltas = [int(item["numa_memory_delta_bytes"]) for item in samples
                   if item["numa_memory_delta_bytes"] is not None]
    cgroup_deltas = [int(item["cgroup_memory_delta_bytes"]) for item in samples
                     if item["cgroup_memory_delta_bytes"] is not None]
    session_wall_values = [float(item["session_wall_s"]) for item in results]
    admission_wait_events = [
        float(wait_s) for item in results
        for wait_s in item.get("admission_wait_events_s", [])
    ]
    tool_reservation_events = [
        event for item in results for event in item.get("tool_reservation_events", [])
    ]
    tool_materialization_events = [
        event for item in results
        for event in item.get("tool_materialization_events", [])
    ]
    tool_reservation_amounts = [
        float(event["reservation_mib"]) for event in tool_reservation_events
    ]
    tool_reservation_waits = [
        float(event["wait_s"]) for event in tool_reservation_events
    ]
    tool_reservation_time_mib_s = sum(
        float(event["reservation_mib"]) * float(event.get("held_s") or 0.0)
        for event in tool_reservation_events
    )
    tool_working_sets = [
        row for item in results for row in item.get("tool_working_sets", [])
    ]
    actual_tool_memory_mib = [
        int(row["actual_command_peak_memory_bytes"]) / (1024.0 * 1024.0)
        for row in tool_working_sets
    ]
    actual_tool_working_set_mib = [
        float(a.idle_tool_vm_rss_mib or 0.0) + value
        for value in actual_tool_memory_mib
    ]
    prediction_rows = [
        row for row in tool_working_sets
        if row.get("predicted_command_memory_p90_mib") is not None
    ]
    reclamation_events = [
        row for item in results
        for row in item.get("checkpoint_reclamation_events", [])
    ]
    balloon_events = [
        row for item in results for row in item.get("balloon_events", [])
    ]
    balloon_reclamation_events = [
        row for row in balloon_events
        if row.get("reason") == "tool_end_idle_reclaim"
    ]
    rss_time = sum(
        rss_values[index] * max(
            0.0, float(samples[index]["elapsed_s"]) - float(samples[index - 1]["elapsed_s"])
        )
        for index in range(1, len(samples))
    )
    tool_admission_metrics = (
        tool_admission.metrics() if tool_admission is not None else None
    )
    report = {"mode": "snapshot" if a.residency_policy == "llm_wait_checkpoint" else "resident",
              "residency_policy": a.residency_policy, "inference": a.inference,
              "numa_node": numa_node,
              "sessions_requested": len(raw["sessions"]), "sessions_completed": len(results),
              "configured_pair_memory_mib": pair_memory_mib,
              "resident_memory_budget_mib": a.resident_memory_budget_mib,
              "resident_pair_slots": resident_pair_slots,
              "cpu_pair_leasing": configured_cpu_pairs is not None,
              "cpu_pair_pool": configured_cpu_pairs,
              "tool_reservation_budget_mib": a.tool_reservation_budget_mib,
              "tool_reservation_policy": (
                  "per_tool_incremental_p90_with_rss_feedback" if a.tool_memory_plan is not None
                  else "static" if a.static_tool_reservation_mib is not None else None
              ),
              "tool_admission_safety_headroom_mib": (
                  a.tool_admission_safety_headroom_mib
                  if tool_admission is not None else None
              ),
              "tool_admission_feedback": (
                  tool_admission_metrics
              ),
              "peak_tool_resident_rss_bytes": (
                  tool_admission_metrics["peak_resident_bytes"]
                  if tool_admission_metrics is not None else None
              ),
              "peak_tool_admission_charge_bytes": (
                  tool_admission_metrics["peak_admission_charge_bytes"]
                  if tool_admission_metrics is not None else None
              ),
              "tool_admission_over_budget_observations": (
                  tool_admission_metrics["over_budget_observations"]
                  if tool_admission_metrics is not None else None
              ),
              "tool_resident_plus_headroom_over_budget_observations": sum(
                  bool(sample.get("tool_resident_plus_headroom_over_budget"))
                  for sample in samples
              ) if tool_admission_metrics is not None else None,
              "tool_prediction_exceeded_leases": (
                  tool_admission_metrics["prediction_exceeded_leases"]
                  if tool_admission_metrics is not None else None
              ),
              "tool_reservation_events": len(tool_reservation_events),
              "tool_materialization_admission_events": len(tool_materialization_events),
              "tool_materialization_admission_wait_s": sum(
                  float(event.get("wait_s") or 0.0)
                  for event in tool_materialization_events
              ),
              "max_tool_materialization_admission_wait_s": max(
                  (float(event.get("wait_s") or 0.0)
                   for event in tool_materialization_events),
                  default=None,
              ),
              "tool_reservation_distinct_mib": sorted(set(tool_reservation_amounts)),
              "mean_tool_reservation_mib": (
                  statistics.fmean(tool_reservation_amounts)
                  if tool_reservation_amounts else None
              ),
              "max_tool_reservation_mib": max(tool_reservation_amounts, default=None),
              "tool_reservation_wait_s": sum(tool_reservation_waits),
              "max_tool_reservation_wait_s": max(tool_reservation_waits, default=None),
              "tool_reservation_time_mib_s": tool_reservation_time_mib_s,
              "tool_balloon_reclamation": a.tool_balloon_reclamation,
              "tool_balloon_idle_floor_mib": (
                  a.tool_balloon_idle_floor_mib
                  if a.tool_balloon_reclamation else None
              ),
              "tool_balloon_events": len(balloon_events),
              "tool_balloon_reclamation_events": len(balloon_reclamation_events),
              "tool_balloon_target_reached_events": sum(
                  bool(row.get("target_reached")) for row in balloon_events
              ),
              "tool_balloon_operation_s": sum(
                  float(row.get("operation_s") or 0.0) for row in balloon_events
              ),
              "tool_balloon_verified_rss_released_bytes": sum(
                  int(row.get("tool_firecracker_rss_released_bytes") or 0)
                  for row in balloon_reclamation_events
              ),
              "tool_working_set_observations": len(tool_working_sets),
              "max_actual_tool_command_memory_mib": max(actual_tool_memory_mib, default=None),
              "mean_actual_tool_command_memory_mib": (
                  statistics.fmean(actual_tool_memory_mib)
                  if actual_tool_memory_mib else None
              ),
              "idle_tool_vm_rss_mib": a.idle_tool_vm_rss_mib,
              "max_actual_tool_working_set_mib": max(
                  actual_tool_working_set_mib, default=None
              ),
              "prediction_observations": len(prediction_rows),
              "prediction_memory_coverage_fraction": (
                  sum(bool(row.get("prediction_covered_actual")) for row in prediction_rows)
                  / len(prediction_rows) if prediction_rows else None
              ),
              "mean_prediction_error_mib": (
                  statistics.fmean(float(row["prediction_error_mib"]) for row in prediction_rows)
                  if prediction_rows else None
              ),
              "fixed_tool_capacity_sufficient": (
                  max(actual_tool_working_set_mib, default=0) < tool_memory_mib
                  if actual_tool_working_set_mib else None
              ),
              "failures": failures, "wall_s": wall_s,
              "throughput_sessions_per_hour": len(results) * 3600 / wall_s,
              "throughput_tasks_per_minute": len(results) * 60 / wall_s,
              "correctness_command": a.correctness_command,
              "correctness_evaluated": bool(a.correctness_command),
              "correct_sessions_completed": correct_sessions,
              "correctness_pass_fraction": (
                  correct_sessions / len(results)
                  if a.correctness_command and results else None
              ),
              "throughput_correct_tasks_per_minute": (
                  correct_sessions * 60 / wall_s if a.correctness_command else None
              ),
              "model_steps_completed": model_steps,
              "replay_requests_input_validated": validated_requests,
              "replay_requests_input_unvalidated": model_steps - validated_requests,
              "replay_input_validation_complete": validated_requests == model_steps,
              "checkpoint_cycles": sum(int(item.get("checkpoint_cycles", 0)) for item in results),
              "vm_snapshot_operations": sum(int(item.get("vm_snapshot_operations", 0)) for item in results),
              "vm_restore_operations": sum(int(item.get("vm_restore_operations", 0)) for item in results),
              "checkpoint_snapshot_service_s": sum(float(item.get("snapshot_s", 0.0)) for item in results),
              "checkpoint_restore_service_s": sum(float(item.get("restore_s", 0.0)) for item in results),
              "checkpoint_reclamation_observations": len(reclamation_events),
              "checkpoint_verified_firecracker_rss_released_bytes": sum(
                  int(row["verified_pair_firecracker_rss_released_bytes"])
                  for row in reclamation_events
              ),
              "checkpoint_cgroup_memory_released_bytes": sum(
                  int(row["cgroup_memory_released_bytes"])
                  for row in reclamation_events
                  if row.get("cgroup_memory_released_bytes") is not None
              ),
              "checkpoint_numa_memory_released_bytes": sum(
                  int(row["numa_memory_released_bytes"])
                  for row in reclamation_events
                  if row.get("numa_memory_released_bytes") is not None
              ),
              "admission_wait_s": sum(float(item.get("admission_wait_s", 0.0)) for item in results),
              "admission_acquisitions": sum(int(item.get("admission_acquisitions", 0)) for item in results),
              "mean_admission_wait_event_s": (
                  statistics.fmean(admission_wait_events) if admission_wait_events else None
              ),
              "p95_admission_wait_event_s": (
                  percentile(admission_wait_events, 0.95) if admission_wait_events else None
              ),
              "max_admission_wait_event_s": max(admission_wait_events, default=None),
              "mean_session_wall_s": (
                  statistics.fmean(session_wall_values) if session_wall_values else None
              ),
              "p50_session_wall_s": (
                  percentile(session_wall_values, 0.50) if session_wall_values else None
              ),
              "p95_session_wall_s": (
                  percentile(session_wall_values, 0.95) if session_wall_values else None
              ),
              "p99_session_wall_s": (
                  percentile(session_wall_values, 0.99) if session_wall_values else None
              ),
              "snapshot_allocated_bytes": sum(
                  int(item.get("snapshot_allocated_bytes", 0)) for item in results
              ),
              "throughput_steps_per_minute": model_steps * 60 / wall_s,
              "mean_firecracker_rss_bytes": statistics.fmean(rss_values) if rss_values else 0,
              "peak_firecracker_rss_bytes": max((x["firecracker_rss_bytes"] for x in samples), default=0),
              "p95_firecracker_rss_bytes": percentile(rss_values, 0.95),
              "firecracker_rss_time_byte_seconds": rss_time,
              "peak_resident_vms": max((x["resident_vms"] for x in samples), default=0),
              "numa_memory_baseline_bytes": numa_baseline,
              "mean_numa_memory_used_bytes": statistics.fmean(numa_values) if numa_values else None,
              "peak_numa_memory_used_bytes": max(numa_values, default=None),
              "mean_numa_memory_delta_bytes": statistics.fmean(numa_deltas) if numa_deltas else None,
              "peak_numa_memory_delta_bytes": max(numa_deltas, default=None),
              "cgroup_v2_path": str(cgroup_path) if cgroup_path is not None else None,
              "cgroup_memory_baseline_bytes": cgroup_baseline,
              "mean_cgroup_memory_delta_bytes": (
                  statistics.fmean(cgroup_deltas) if cgroup_deltas else None
              ),
              "peak_cgroup_memory_delta_bytes": max(cgroup_deltas, default=None),
              "sessions": results}
    (a.output / "memory.json").write_text(json.dumps(samples, indent=2) + "\n")
    (a.output / "summary.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__": main()
