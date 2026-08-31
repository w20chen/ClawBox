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
from typing import Any, Callable, NamedTuple
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clawbox.replay.lifecycle import (
    FairResourcePool, FeedbackMemoryAdmission, FirecrackerConfig, FirecrackerLifecycle,
)
from clawbox.replay.model_gateway import ModelGateway


TOOL_VM_CONTRACT_MIB = 4096


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


def cgroup_value(path: Path, name: str) -> int | None:
    try:
        value = (path / name).read_text(encoding="ascii").strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


def cgroup_memory_events(path: Path) -> dict[str, int]:
    try:
        return {
            fields[0]: int(fields[1])
            for line in (path / "memory.events").read_text(encoding="ascii").splitlines()
            if len(fields := line.split()) == 2
        }
    except (OSError, ValueError):
        return {}


def guest_oom_observed(log_path: Path | None) -> bool:
    if log_path is None:
        return False
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(marker in text for marker in (
        "out of memory: killed process", "oom-kill:",
        "kernel panic - not syncing: system is deadlocked on memory",
    ))


def validate_memory_cgroup(path: Path, hard_limit_mib: int, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not (resolved / "cgroup.procs").is_file():
        raise ValueError(f"{label} is not a cgroup v2 directory: {resolved}")
    configured = cgroup_value(resolved, "memory.max")
    expected = int(hard_limit_mib) * 1024 * 1024
    if configured != expected:
        rendered = "max" if configured is None else str(configured)
        raise ValueError(
            f"{label} memory.max is {rendered}; expected exactly {expected} bytes"
        )
    return resolved


def validate_cgroup_directory(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not (resolved / "cgroup.procs").is_file():
        raise ValueError(f"{label} is not a cgroup v2 directory: {resolved}")
    return resolved


def validate_tool_pool_cgroup(path: Path, hard_limit_mib: int) -> Path:
    return validate_memory_cgroup(path, hard_limit_mib, "Tool pool")


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


class CheckpointTransition(NamedTuple):
    scope: str
    elapsed_s: float
    runtime_rss_before_bytes: int
    runtime_rss_after_bytes: int
    tool_rss_before_bytes: int
    tool_rss_after_bytes: int
    host_before: dict[str, int | None]
    host_after: dict[str, int | None]


class PairCheckpointCoordinator:
    """Serialize response delivery, checkpoint, and restore for one sandbox.

    Firecracker snapshots preserve guest process and network-device state.  The
    coordinator supplies the missing host-side atomicity: response delivery can
    either win before eviction begins or wait until the dependency-safe restore
    has completed, but it can never race a partially evicted pair.
    """

    def __init__(
        self,
        runtime: FirecrackerLifecycle,
        tool: FirecrackerLifecycle,
        wait_tool_ready: Callable[[], None],
        *,
        checkpoint_scope: str = "pair",
    ) -> None:
        if checkpoint_scope not in {"pair", "tool"}:
            raise ValueError("checkpoint_scope must be pair or tool")
        self.runtime = runtime
        self.tool = tool
        self.wait_tool_ready = wait_tool_ready
        self.checkpoint_scope = checkpoint_scope
        self._condition = threading.Condition()
        self._state = "running"
        self._response_delivery_started = False
        self._failure: Exception | None = None
        self._restore_total_bytes = 0
        self._restore_tool_bytes = 0

    @property
    def state(self) -> str:
        with self._condition:
            return self._state

    def start_request(self) -> None:
        with self._condition:
            if self._state != "running":
                raise RuntimeError(
                    f"new model request started while checkpoint state is {self._state}"
                )
            self._response_delivery_started = False

    def begin_response_delivery(self) -> None:
        with self._condition:
            self._response_delivery_started = True
            self._condition.notify_all()

    def evict(
        self,
        observe_host: Callable[[], dict[str, int | None]],
    ) -> CheckpointTransition | None:
        with self._condition:
            if self._state != "running" or self._response_delivery_started:
                return None
            self._state = "evicting"

        runtime_before = self.runtime.rss_bytes()
        tool_before = self.tool.rss_bytes()
        host_before = observe_host()
        started = time.monotonic()
        try:
            if self.checkpoint_scope == "pair":
                checkpoint_runtime_tool_pair(self.runtime, self.tool)
            else:
                self.tool.checkpoint_and_evict()
            elapsed = time.monotonic() - started
            runtime_after = self.runtime.rss_bytes()
            tool_after = self.tool.rss_bytes()
            if tool_after != 0 or (
                self.checkpoint_scope == "pair" and runtime_after != 0
            ):
                raise RuntimeError(
                    f"{self.checkpoint_scope} checkpoint did not release expected Firecracker RSS"
                )
            host_after = observe_host()
        except Exception as exc:
            with self._condition:
                self._failure = exc
                self._state = "failed"
                self._condition.notify_all()
            raise

        transition = CheckpointTransition(
            scope=self.checkpoint_scope,
            elapsed_s=elapsed,
            runtime_rss_before_bytes=runtime_before,
            runtime_rss_after_bytes=runtime_after,
            tool_rss_before_bytes=tool_before,
            tool_rss_after_bytes=tool_after,
            host_before=host_before,
            host_after=host_after,
        )
        with self._condition:
            self._restore_total_bytes = (
                tool_before
                + (runtime_before if self.checkpoint_scope == "pair" else 0)
            )
            self._restore_tool_bytes = tool_before
            self._state = "evicted"
            self._condition.notify_all()
        return transition

    def restore(
        self,
        admission: Callable[[int, int, Callable[[], None]], None] | None = None,
    ) -> dict[str, Any] | None:
        with self._condition:
            while self._state in {"evicting", "restoring"}:
                self._condition.wait()
            if self._state == "failed":
                raise RuntimeError("checkpoint coordinator failed") from self._failure
            if self._state == "running":
                return None
            if self._state != "evicted":
                raise RuntimeError(f"cannot restore checkpoint state {self._state}")
            self._state = "restoring"

        started = time.monotonic()
        try:
            def restore_operation() -> None:
                if self.checkpoint_scope == "pair":
                    restore_tool_runtime_pair(
                        self.tool, self.runtime, self.wait_tool_ready,
                    )
                else:
                    self.tool.restore()
                    self.wait_tool_ready()

            if admission is None:
                restore_operation()
            else:
                admission(
                    self._restore_total_bytes,
                    self._restore_tool_bytes,
                    restore_operation,
                )
            runtime_restored = self.checkpoint_scope == "pair"
            elapsed = time.monotonic() - started
        except Exception as exc:
            with self._condition:
                self._failure = exc
                self._state = "failed"
                self._condition.notify_all()
            raise

        with self._condition:
            self._state = "running"
            self._condition.notify_all()
        return {
            "checkpoint_scope": self.checkpoint_scope,
            "restore_s": elapsed,
            "runtime_restored": runtime_restored,
            "tool_restored": True,
            "restore_total_reservation_bytes": self._restore_total_bytes,
            "restore_tool_reservation_bytes": self._restore_tool_bytes,
        }


class IdleSandboxCandidate(NamedTuple):
    session_id: int
    request_id: str
    idle_since_unix_s: float
    predicted_wait_s: float
    evict: Callable[[], None]


class LruIdleSandboxRegistry:
    """Select exactly one deterministic LRU victim from idle LLM waiters."""

    def __init__(
        self,
        *,
        eviction_policy: str,
        fixed_delay_s: float,
        checkpoint_break_even_s: float,
        pressure_active: Callable[[], bool],
        poll_s: float = 0.05,
    ) -> None:
        if eviction_policy not in {"eager", "fixed_delay", "predicted_pressure_aware"}:
            raise ValueError("invalid eviction policy")
        self.eviction_policy = eviction_policy
        self.fixed_delay_s = max(0.0, float(fixed_delay_s))
        self.checkpoint_break_even_s = max(0.0, float(checkpoint_break_even_s))
        self.pressure_active = pressure_active
        self.poll_s = max(0.001, float(poll_s))
        self._condition = threading.Condition()
        self._candidates: dict[int, IdleSandboxCandidate] = {}
        self._closed = False

    def register(self, candidate: IdleSandboxCandidate) -> None:
        with self._condition:
            current = self._candidates.get(candidate.session_id)
            if current is not None and current.request_id == candidate.request_id:
                return
            self._candidates[candidate.session_id] = candidate
            self._condition.notify_all()

    def unregister(self, session_id: int, request_id: str | None = None) -> None:
        with self._condition:
            current = self._candidates.get(session_id)
            if current is not None and (
                request_id is None or current.request_id == request_id
            ):
                self._candidates.pop(session_id, None)
                self._condition.notify_all()

    def select_lru(self, now_unix_s: float) -> IdleSandboxCandidate | None:
        with self._condition:
            pressure = self.pressure_active()
            eligible = [
                candidate for candidate in self._candidates.values()
                if self._eligible(candidate, now_unix_s, pressure)
            ]
            if not eligible:
                return None
            return min(
                eligible,
                key=lambda item: (item.idle_since_unix_s, item.session_id),
            )

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def run(self) -> None:
        while True:
            with self._condition:
                while True:
                    if self._closed:
                        return
                    victim = self.select_lru(time.time())
                    if victim is not None:
                        self._candidates.pop(victim.session_id, None)
                        break
                    self._condition.wait(self.poll_s)
            victim.evict()

    def _eligible(
        self,
        candidate: IdleSandboxCandidate,
        now_unix_s: float,
        pressure: bool,
    ) -> bool:
        elapsed = max(0.0, now_unix_s - candidate.idle_since_unix_s)
        if self.eviction_policy == "eager":
            return True
        if self.eviction_policy == "fixed_delay":
            return elapsed >= self.fixed_delay_s
        return pressure or candidate.predicted_wait_s - elapsed >= self.checkpoint_break_even_s


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


def oracle_steps_from_plan(plan_payload: dict, workload: str) -> dict[int, dict]:
    """Load held-out actual incremental working sets for the oracle upper bound."""
    workload_plan = plan_payload["per_tool_memory"]["workloads"][workload]
    steps: dict[int, dict] = {}
    for invocation in workload_plan["tool_invocations"]:
        step = int(invocation["model_step"])
        actual_kib = invocation.get("actual_incremental_kib")
        if actual_kib is None and invocation.get("actual_command_peak_memory_bytes") is not None:
            actual_kib = math.ceil(int(invocation["actual_command_peak_memory_bytes"]) / 1024.0)
        if actual_kib is None:
            raise ValueError(
                "oracle plan requires actual_incremental_kib or "
                "actual_command_peak_memory_bytes for every Tool invocation"
            )
        current = steps.setdefault(step, {
            "incremental_oracle_kib": 0, "tool_invocations": [],
        })
        current["incremental_oracle_kib"] += max(1, int(actual_kib))
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


def balloon_materialization_reclaim_verified(
    event: dict, idle_tool_vm_rss_mib: float,
) -> tuple[bool, int]:
    """Require both guest progress and bounded host RSS before slot release."""
    release_limit_mib = math.ceil(float(idle_tool_vm_rss_mib) * 1.5)
    verified = (
        bool(event.get("target_reached"))
        and int(event.get("tool_firecracker_rss_after_bytes") or 0)
        <= release_limit_mib * 1024 * 1024
    )
    return verified, release_limit_mib


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("manifest", type=Path)
    p.add_argument("--output", required=True, type=Path)
    residency = p.add_mutually_exclusive_group(required=True)
    residency.add_argument(
        "--reclamation-policy",
        choices=("resident", "balloon", "checkpoint", "hybrid"),
    )
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
    p.add_argument(
        "--admission-policy",
        choices=("full_reservation", "static", "p90", "oracle"),
    )
    p.add_argument("--idle-tool-vm-rss-mib", type=float)
    p.add_argument("--static-tool-reservation-mib", type=int)
    p.add_argument("--tool-memory-plan", type=Path)
    p.add_argument("--tool-memory-workload")
    p.add_argument("--tool-pool-cgroup", type=Path)
    p.add_argument("--tool-pool-hard-limit-mib", type=int)
    p.add_argument("--tool-pool-low-watermark-mib", type=int)
    p.add_argument("--vm-pool-cgroup", type=Path)
    p.add_argument("--runtime-pool-cgroup", type=Path)
    p.add_argument("--vm-pool-hard-limit-mib", type=int)
    p.add_argument("--vm-pool-high-watermark-mib", type=int)
    p.add_argument("--vm-pool-low-watermark-mib", type=int)
    p.add_argument("--vm-pool-headroom-mib", type=int)
    p.add_argument("--initial-runtime-rss-reservation-mib", type=int, default=256)
    p.add_argument("--initial-tool-rss-reservation-mib", type=int, default=256)
    p.add_argument("--restore-transient-headroom-mib", type=int, default=256)
    p.add_argument("--tool-balloon-reclamation", action="store_true")
    p.add_argument("--tool-balloon-idle-floor-mib", type=int)
    p.add_argument("--tool-balloon-settle-timeout-s", type=float, default=5.0)
    p.add_argument(
        "--eviction-policy",
        choices=("eager", "fixed_delay", "predicted_pressure_aware"),
        default="fixed_delay",
    )
    p.add_argument("--eviction-delay-s", type=float, default=20.0)
    p.add_argument("--predicted-llm-wait-s", type=float, default=20.0)
    p.add_argument("--checkpoint-break-even-s", type=float, default=4.0)
    p.add_argument(
        "--restore-policy", choices=("reactive", "prefetch"), default="reactive",
    )
    p.add_argument("--restore-prefetch-lead-s", type=float, default=2.0)
    p.add_argument(
        "--checkpoint-scope", choices=("pair", "tool"), default="pair",
        help="checkpoint Runtime+Tool by default; tool is a secondary ablation",
    )
    a = p.parse_args()
    if a.timeout_s <= 0:
        p.error("--timeout-s must be positive")
    if a.correctness_timeout_s <= 0:
        p.error("--correctness-timeout-s must be positive")
    if a.time_scale < 0:
        p.error("--time-scale must be non-negative")
    if a.resident_memory_budget_mib is not None and a.resident_memory_budget_mib <= 0:
        p.error("--resident-memory-budget-mib must be positive")
    vm_pool_values = (
        a.vm_pool_cgroup,
        a.runtime_pool_cgroup,
        a.vm_pool_hard_limit_mib,
        a.vm_pool_high_watermark_mib,
        a.vm_pool_low_watermark_mib,
        a.vm_pool_headroom_mib,
    )
    if any(value is not None for value in vm_pool_values) and not all(
        value is not None for value in vm_pool_values
    ):
        p.error("VM-pool admission requires both cgroups, H, Whigh, Wlow, and headroom")
    if a.tool_reservation_budget_mib is not None and not all(
        value is not None for value in vm_pool_values
    ):
        p.error("paper Tool admission requires VM-pool memory admission")
    if a.vm_pool_high_watermark_mib is not None:
        if not (
            0
            < a.vm_pool_low_watermark_mib
            < a.vm_pool_high_watermark_mib
            < a.vm_pool_hard_limit_mib
        ):
            p.error("VM-pool watermarks must satisfy 0 < Wlow < Whigh < H")
        if not 0 <= a.vm_pool_headroom_mib < a.vm_pool_high_watermark_mib:
            p.error("VM-pool headroom must be non-negative and below Whigh")
        if a.initial_runtime_rss_reservation_mib <= 0:
            p.error("initial Runtime RSS reservation must be positive")
        if a.initial_tool_rss_reservation_mib <= 0:
            p.error("initial Tool RSS reservation must be positive")
        if a.restore_transient_headroom_mib < 0:
            p.error("restore transient headroom must be non-negative")
    if a.tool_reservation_budget_mib is not None and a.tool_reservation_budget_mib <= 0:
        p.error("--tool-reservation-budget-mib must be positive")
    if a.tool_reservation_budget_mib is not None:
        if a.admission_policy is None:
            # Compatibility for older direct invocations.
            a.admission_policy = "p90" if a.tool_memory_plan is not None else "full_reservation"
        if a.admission_policy in {"p90", "oracle"} and a.tool_memory_plan is None:
            p.error(f"{a.admission_policy} admission requires --tool-memory-plan")
        if a.admission_policy == "static" and a.static_tool_reservation_mib is None:
            p.error("static admission requires --static-tool-reservation-mib")
        if a.admission_policy == "full_reservation":
            a.static_tool_reservation_mib = TOOL_VM_CONTRACT_MIB
        if a.admission_policy in {"full_reservation", "static"} and a.tool_memory_plan is not None:
            p.error(f"{a.admission_policy} admission does not use a Tool memory plan")
        if (a.static_tool_reservation_mib is not None
                and a.static_tool_reservation_mib > TOOL_VM_CONTRACT_MIB):
            p.error("static Tool reservation exceeds the 4 GiB tenant contract")
        if (a.tool_admission_safety_headroom_mib < 0
                or a.tool_admission_safety_headroom_mib >= a.tool_reservation_budget_mib):
            p.error("Tool admission safety headroom must be non-negative and below budget")
        if a.tool_pool_cgroup is None or a.tool_pool_hard_limit_mib is None:
            p.error("Tool admission requires a cgroup and its fixed hard limit")
        if a.tool_pool_hard_limit_mib <= a.tool_reservation_budget_mib:
            p.error("Tool pool hard limit must be above the high admission watermark")
        if (a.tool_pool_low_watermark_mib is None
                or not 0 < a.tool_pool_low_watermark_mib < a.tool_reservation_budget_mib):
            p.error("Tool pool low watermark must be positive and below the high watermark")
        if a.vm_pool_hard_limit_mib <= a.tool_pool_hard_limit_mib:
            p.error("VM-pool hard limit must exceed the Tool-pool hard limit")
        if (
            a.initial_tool_rss_reservation_mib
            + a.tool_admission_safety_headroom_mib
            > a.tool_reservation_budget_mib
        ):
            p.error("initial Tool RSS reservation does not fit Tool Whigh")
        if (
            max(a.initial_runtime_rss_reservation_mib,
                a.initial_tool_rss_reservation_mib)
            + a.vm_pool_headroom_mib
            > a.vm_pool_high_watermark_mib
        ):
            p.error("initial VM RSS reservation does not fit VM Whigh")
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
    a.reclamation_policy = a.reclamation_policy or (
        "checkpoint" if a.residency_policy == "llm_wait_checkpoint" else "resident"
    )
    a.tool_balloon_reclamation = a.tool_balloon_reclamation or (
        a.reclamation_policy in {"balloon", "hybrid"}
    )
    if a.tool_balloon_reclamation:
        if (a.tool_balloon_idle_floor_mib is None
                or a.tool_balloon_idle_floor_mib <= 0):
            p.error("Tool balloon reclamation requires a positive idle floor")
        if a.tool_balloon_settle_timeout_s <= 0:
            p.error("Tool balloon settle timeout must be positive")
    if a.reclamation_policy in {"checkpoint", "hybrid"}:
        if a.eviction_delay_s < 0 or a.predicted_llm_wait_s < 0:
            p.error("eviction delay and predicted LLM wait must be non-negative")
        if a.checkpoint_break_even_s < 0 or a.restore_prefetch_lead_s < 0:
            p.error("checkpoint break-even and prefetch lead must be non-negative")
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
    if tool_memory_mib != TOOL_VM_CONTRACT_MIB:
        raise ValueError(
            f"paper experiments require every Tool VM to expose exactly "
            f"{TOOL_VM_CONTRACT_MIB} MiB; got {tool_memory_mib} MiB"
        )
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
        predictive_steps = (
            oracle_steps_from_plan(plan_payload, a.tool_memory_workload)
            if a.admission_policy == "oracle"
            else predictive_steps_from_plan(plan_payload, a.tool_memory_workload)
        )
    tool_pool_cgroup = None
    tool_pool_events_before: dict[str, int] = {}
    vm_pool_cgroup = None
    runtime_pool_cgroup = None
    vm_pool_events_before: dict[str, int] = {}
    if a.tool_reservation_budget_mib is not None:
        tool_pool_cgroup = validate_tool_pool_cgroup(
            a.tool_pool_cgroup, a.tool_pool_hard_limit_mib,
        )
        tool_pool_events_before = cgroup_memory_events(tool_pool_cgroup)
        vm_pool_cgroup = validate_memory_cgroup(
            a.vm_pool_cgroup, a.vm_pool_hard_limit_mib, "VM pool",
        )
        runtime_pool_cgroup = validate_cgroup_directory(
            a.runtime_pool_cgroup, "Runtime pool",
        )
        if tool_pool_cgroup.parent != vm_pool_cgroup:
            raise ValueError("Tool pool cgroup must be a direct child of the VM pool")
        if runtime_pool_cgroup.parent != vm_pool_cgroup:
            raise ValueError("Runtime pool cgroup must be a direct child of the VM pool")
        if runtime_pool_cgroup == tool_pool_cgroup:
            raise ValueError("Runtime and Tool pools must use distinct child cgroups")
        vm_pool_events_before = cgroup_memory_events(vm_pool_cgroup)
    a.output.mkdir(parents=True, exist_ok=False)
    lifecycles: list[FirecrackerLifecycle] = []
    tool_lifecycles: list[FirecrackerLifecycle] = []
    runtime_lifecycles: list[FirecrackerLifecycle] = []
    lifecycle_lock = threading.Lock()
    def measure_tool_resident_bytes() -> int:
        with lifecycle_lock:
            rss = sum(item.rss_bytes() for item in tool_lifecycles)
        charged = (
            cgroup_value(tool_pool_cgroup, "memory.current")
            if tool_pool_cgroup is not None else None
        )
        return max(rss, int(charged or 0))
    def measure_vm_resident_bytes() -> int:
        with lifecycle_lock:
            rss = sum(item.rss_bytes() for item in lifecycles)
        charged = (
            cgroup_value(vm_pool_cgroup, "memory.current")
            if vm_pool_cgroup is not None else None
        )
        return max(rss, int(charged or 0))
    tool_admission = (
        FeedbackMemoryAdmission(
            a.tool_reservation_budget_mib * 1024 * 1024,
            a.tool_admission_safety_headroom_mib * 1024 * 1024,
            measure_tool_resident_bytes,
        )
        if a.tool_reservation_budget_mib is not None else None
    )
    vm_admission = (
        FeedbackMemoryAdmission(
            a.vm_pool_high_watermark_mib * 1024 * 1024,
            a.vm_pool_headroom_mib * 1024 * 1024,
            measure_vm_resident_bytes,
        )
        if a.vm_pool_high_watermark_mib is not None else None
    )
    samples: list[dict] = []
    stop = threading.Event()
    pressure_reclaim_active = threading.Event()
    idle_sandboxes = LruIdleSandboxRegistry(
        eviction_policy=a.eviction_policy,
        fixed_delay_s=a.eviction_delay_s,
        checkpoint_break_even_s=a.checkpoint_break_even_s,
        pressure_active=pressure_reclaim_active.is_set,
    )
    victim_selector = threading.Thread(target=idle_sandboxes.run, daemon=True)
    victim_selector.start()
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
                runtime_rss = sum(item.rss_bytes() for item in runtime_lifecycles)
                resident_vms = sum(1 for item in lifecycles if item.resident)
            tool_pool_current = (
                cgroup_value(tool_pool_cgroup, "memory.current")
                if tool_pool_cgroup is not None else None
            )
            vm_pool_current = (
                cgroup_value(vm_pool_cgroup, "memory.current")
                if vm_pool_cgroup is not None else None
            )
            tool_charged = max(tool_rss, int(tool_pool_current or 0))
            vm_charged = max(rss, int(vm_pool_current or 0))
            admission_status = (
                tool_admission.observe(tool_charged)
                if tool_admission is not None else None
            )
            vm_admission_status = (
                vm_admission.observe(vm_charged) if vm_admission is not None else None
            )
            if tool_admission is not None:
                tool_high_bytes = int(a.tool_reservation_budget_mib) * 1024 * 1024
                tool_low_bytes = int(a.tool_pool_low_watermark_mib) * 1024 * 1024
                vm_high_bytes = int(a.vm_pool_high_watermark_mib) * 1024 * 1024
                vm_low_bytes = int(a.vm_pool_low_watermark_mib) * 1024 * 1024
                if tool_charged >= tool_high_bytes or vm_charged >= vm_high_bytes:
                    pressure_reclaim_active.set()
                elif tool_charged <= tool_low_bytes and vm_charged <= vm_low_bytes:
                    pressure_reclaim_active.clear()
            cgroup_memory = cgroup_memory_used_bytes()
            numa_memory = numa_memory_used_bytes(numa_node)
            samples.append({
                "elapsed_s": time.monotonic() - started,
                "firecracker_rss_bytes": rss,
                "tool_firecracker_rss_bytes": tool_rss,
                "runtime_firecracker_rss_bytes": runtime_rss,
                "tool_pool_memory_current_bytes": tool_pool_current,
                "vm_pool_memory_current_bytes": vm_pool_current,
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
                "vm_admission": vm_admission_status,
                "tool_resident_plus_headroom_over_budget": (
                    admission_status is not None
                    and tool_charged + admission_status["safety_headroom_bytes"]
                    > admission_status["budget_bytes"]
                ),
                "vm_resident_plus_headroom_over_budget": (
                    vm_admission_status is not None
                    and vm_charged + vm_admission_status["safety_headroom_bytes"]
                    > vm_admission_status["budget_bytes"]
                ),
            })
    sampler = threading.Thread(target=sample, daemon=True); sampler.start()

    def run_one(index: int, spec: dict) -> dict:
        session_started_elapsed_s = time.monotonic() - started
        tool_config, runtime_config = FirecrackerConfig.from_json(Path(spec["tool"])), FirecrackerConfig.from_json(Path(spec["runtime"]))
        remaining = lambda: max(0.0, deadline - time.monotonic())
        tool = FirecrackerLifecycle(tool_config)
        runtime = FirecrackerLifecycle(runtime_config)
        if tool_pool_cgroup is not None:
            tool.config = replace(tool.config, cgroup_path=tool_pool_cgroup)
            runtime.config = replace(runtime.config, cgroup_path=runtime_pool_cgroup)
        if a.tool_balloon_reclamation:
            tool.config = replace(tool.config, balloon_enabled=True)
        with lifecycle_lock:
            lifecycles.extend([tool, runtime])
            tool_lifecycles.append(tool)
            runtime_lifecycles.append(runtime)
        snapshots = 0; snapshot_s = restore_s = 0.0
        admission_wait_s = 0.0
        admission_acquisitions = 0
        admission_wait_events_s: list[float] = []
        admission_leases: list[dict] = []
        pair_lease: object | None = None
        current_lease_event: dict | None = None
        tool_reservation_lease: int | None = None
        vm_tool_growth_lease: int | None = None
        tool_reservation_events: list[dict] = []
        checkpoint_reclamation_events: list[dict] = []
        balloon_events: list[dict] = []
        vm_materialization_events: list[dict] = []
        current_tool_reservation_event: dict | None = None
        tool_reservation_lock = threading.Lock()
        tool_state_lock = threading.Lock()
        reclaim_accounting_lock = threading.Lock()
        idle_reclaim_complete = threading.Event()
        idle_reclaim_complete.set()
        idle_reclaim_errors: list[Exception] = []
        session_closing = threading.Event()
        checkpoint_coordinator = PairCheckpointCoordinator(
            runtime,
            tool,
            lambda: wait_tcp(spec["tool_host"], 2222, 30),
            checkpoint_scope=a.checkpoint_scope,
        )

        def with_memory_growth_admission(
            reason: str,
            total_incremental_bytes: int,
            tool_incremental_bytes: int,
            operation: Callable[[], Any],
        ) -> Any:
            """Reserve transient RSS growth, then fall back to measured RSS."""
            total_incremental_bytes = max(1, int(total_incremental_bytes))
            tool_incremental_bytes = max(0, int(tool_incremental_bytes))
            wait_started = time.monotonic()
            vm_acquired = (
                vm_admission.acquire(
                    total_incremental_bytes,
                    timeout=remaining(),
                    measure_resident_bytes=lambda: (
                        runtime.rss_bytes() + tool.rss_bytes()
                    ),
                )
                if vm_admission is not None else None
            )
            if vm_admission is not None and vm_acquired is None:
                raise TimeoutError(f"timed out waiting for VM-pool {reason} admission")
            vm_lease = vm_acquired[0] if vm_acquired is not None else None
            tool_acquired = None
            try:
                if tool_admission is not None and tool_incremental_bytes > 0:
                    tool_acquired = tool_admission.acquire(
                        tool_incremental_bytes,
                        timeout=remaining(),
                        measure_resident_bytes=tool.rss_bytes,
                    )
                    if tool_acquired is None:
                        raise TimeoutError(
                            f"timed out waiting for Tool-pool {reason} admission"
                        )
                event = {
                    "reason": reason,
                    "predicted_total_growth_mib": (
                        total_incremental_bytes / (1024.0 * 1024.0)
                    ),
                    "predicted_tool_growth_mib": (
                        tool_incremental_bytes / (1024.0 * 1024.0)
                    ),
                    "wait_s": time.monotonic() - wait_started,
                    "acquired_elapsed_s": time.monotonic() - started,
                    "released_elapsed_s": None,
                }
                vm_materialization_events.append(event)
                try:
                    return operation()
                finally:
                    if tool_acquired is not None:
                        tool_status = tool_admission.release(tool_acquired[0])
                        tool_acquired = None
                        event["tool_resident_after_mib"] = (
                            tool_status["resident_bytes"] / (1024.0 * 1024.0)
                        )
                    if vm_lease is not None:
                        vm_status = vm_admission.release(vm_lease)
                        vm_lease = None
                        event["vm_resident_after_mib"] = (
                            vm_status["resident_bytes"] / (1024.0 * 1024.0)
                        )
                    event["released_elapsed_s"] = time.monotonic() - started
                    event["held_s"] = (
                        event["released_elapsed_s"] - event["acquired_elapsed_s"]
                    )
            except Exception:
                if tool_acquired is not None:
                    tool_admission.release(tool_acquired[0])
                if vm_lease is not None:
                    vm_admission.release(vm_lease)
                raise

        def restore_with_memory_admission(
            total_restore_bytes: int,
            tool_restore_bytes: int,
            operation: Callable[[], None],
        ) -> None:
            transient = int(a.restore_transient_headroom_mib) * 1024 * 1024
            with_memory_growth_admission(
                "checkpoint_restore",
                total_restore_bytes + transient,
                tool_restore_bytes + transient,
                operation,
            )

        def release_tool_reservation() -> None:
            nonlocal tool_reservation_lease, vm_tool_growth_lease
            nonlocal current_tool_reservation_event
            with tool_reservation_lock:
                lease = tool_reservation_lease
                tool_reservation_lease = None
                vm_lease = vm_tool_growth_lease
                vm_tool_growth_lease = None
                event = current_tool_reservation_event
                current_tool_reservation_event = None
            if lease is not None:
                assert tool_admission is not None
                released_status = None
                vm_released_status = None
                try:
                    if a.tool_balloon_reclamation and tool.resident:
                        with tool_state_lock:
                            balloon_event = adjust_balloon(
                                tool,
                                tool_memory_mib - int(a.tool_balloon_idle_floor_mib),
                                "tool_end_idle_reclaim",
                                a.tool_balloon_settle_timeout_s,
                            )
                        balloon_events.append(balloon_event)
                        if event is not None:
                            event["balloon_reclamation"] = balloon_event
                finally:
                    try:
                        released_status = tool_admission.release(lease)
                    finally:
                        if vm_admission is not None and vm_lease is not None:
                            vm_released_status = vm_admission.release(vm_lease)
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
                    if vm_released_status is not None:
                        event["vm_remaining_headroom_after_mib"] = (
                            vm_released_status["remaining_headroom_bytes"]
                            / (1024.0 * 1024.0)
                        )

        def before_response_ready(step: int | None, message: dict) -> dict:
            nonlocal tool_reservation_lease, vm_tool_growth_lease
            nonlocal current_tool_reservation_event, restore_s
            # Mark delivery before waiting.  The coordinator then either
            # cancels an eviction that has not started or waits for an in-flight
            # snapshot and restores the dependency-safe pair before bytes are
            # written to the Runtime's HTTP connection.
            idle_sandboxes.unregister(index)
            checkpoint_coordinator.begin_response_delivery()
            if not idle_reclaim_complete.wait(timeout=remaining()):
                raise TimeoutError("timed out waiting for idle sandbox reclamation")
            if idle_reclaim_errors:
                raise RuntimeError("idle sandbox reclamation failed") from idle_reclaim_errors[0]
            restored = checkpoint_coordinator.restore(restore_with_memory_admission)
            if restored is not None:
                restore_s += float(restored["restore_s"])
                checkpoint_reclamation_events.append({
                    "reason": "reactive_response_restore",
                    "model_step": step,
                    **restored,
                })
            tool_calls = message.get("tool_calls") or []
            if not tool_calls or tool_admission is None:
                return {}
            if session_closing.is_set():
                raise RuntimeError("session closed before Tool admission")
            if a.admission_policy in {"full_reservation", "static"}:
                current_tool_rss_kib = math.ceil(tool.rss_bytes() / 1024.0)
                incremental_kib = max(
                    1,
                    int(a.static_tool_reservation_mib) * 1024 - current_tool_rss_kib,
                )
                provenance = {"policy": a.admission_policy,
                              "static_capacity_mib": int(a.static_tool_reservation_mib),
                              "session_tool_rss_before_mib": current_tool_rss_kib / 1024.0,
                              "tool_invocations": tool_call_descriptors(message)}
            else:
                if step is None or step not in predictive_steps:
                    raise RuntimeError(
                        f"{a.admission_policy} plan has no Tool reservation for model step {step}"
                    )
                planned = predictive_steps[step]
                if len(planned["tool_invocations"]) != len(tool_calls):
                    raise RuntimeError(f"{a.admission_policy} plan diverged at model step {step}")
                if a.admission_policy == "oracle":
                    incremental_kib = int(planned["incremental_oracle_kib"])
                    provenance = {"policy": "oracle_incremental_working_set", **planned}
                else:
                    incremental_kib = int(planned["incremental_p90_kib"])
                    provenance = {"policy": "per_tool_incremental_p90", **planned}
            wait_started = time.monotonic()
            vm_acquired = vm_admission.acquire(
                incremental_kib * 1024,
                timeout=remaining(),
                measure_resident_bytes=lambda: (
                    runtime.rss_bytes() + tool.rss_bytes()
                ),
            )
            if vm_acquired is None:
                raise TimeoutError("timed out waiting for VM-pool Tool admission")
            acquired = tool_admission.acquire(
                incremental_kib * 1024,
                timeout=remaining(),
                measure_resident_bytes=tool.rss_bytes,
            )
            wait_s = time.monotonic() - wait_started
            if acquired is None:
                vm_admission.release(vm_acquired[0])
                raise TimeoutError("timed out waiting for live-RSS Tool admission")
            lease, admission_status = acquired
            vm_lease, vm_admission_status = vm_acquired
            event = {
                "model_step": step,
                "reservation_kib": incremental_kib,
                "reservation_mib": incremental_kib / 1024.0,
                "predicted_incremental_p90_mib": (
                    incremental_kib / 1024.0 if a.admission_policy == "p90" else None
                ),
                "oracle_incremental_mib": (
                    incremental_kib / 1024.0 if a.admission_policy == "oracle" else None
                ),
                "resident_tool_rss_before_mib": (
                    admission_status["resident_bytes"] / (1024.0 * 1024.0)
                ),
                "outstanding_unrealized_growth_mib": (
                    admission_status["outstanding_unrealized_growth_bytes"]
                    / (1024.0 * 1024.0)
                ),
                "admission_charge_mib": (
                    admission_status["admission_charge_bytes"] / (1024.0 * 1024.0)
                ),
                "remaining_headroom_mib": (
                    admission_status["remaining_headroom_bytes"] / (1024.0 * 1024.0)
                ),
                "vm_admission_charge_mib": (
                    vm_admission_status["admission_charge_bytes"]
                    / (1024.0 * 1024.0)
                ),
                "vm_remaining_headroom_mib": (
                    vm_admission_status["remaining_headroom_bytes"]
                    / (1024.0 * 1024.0)
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
                    vm_tool_growth_lease = vm_lease
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
                vm_admission.release(vm_lease)
                raise RuntimeError("session closed while waiting for Tool admission")
            return event

        def on_request_started() -> None:
            idle_sandboxes.unregister(index)
            checkpoint_coordinator.start_request()
            idle_reclaim_complete.clear()
            def reclaim_during_wait() -> None:
                try:
                    release_tool_reservation()
                except Exception as exc:
                    idle_reclaim_errors.append(exc)
                finally:
                    idle_reclaim_complete.set()
            threading.Thread(target=reclaim_during_wait, daemon=True).start()

        gateway = ModelGateway(
            Path(spec["store"]), mode=a.inference, trace=a.trace, time_scale=a.time_scale,
            upstream_base_url=a.api_base_url,
            upstream_api_key=os.environ.get(a.api_key_env), upstream_model=a.api_model,
            request_namespace=f"session-{index:04d}",
            on_request_started=on_request_started,
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

        def release_pair() -> None:
            nonlocal pair_lease, current_lease_event
            if pair_lease is None:
                return
            lease, pair_lease = pair_lease, None
            if current_lease_event is not None:
                current_lease_event["released_elapsed_s"] = time.monotonic() - started
                current_lease_event = None
            pair_slots.release(lease)

        def predicted_wait_s(record: dict) -> float:
            replay_index = record.get("replay_index")
            if (a.inference == "replay" and replay_index is not None
                    and 0 <= int(replay_index) < len(gateway.actions)):
                return float(gateway.actions[int(replay_index)].duration_s) * a.time_scale
            return float(a.predicted_llm_wait_s)

        def prefetch_due(record: dict) -> bool:
            if a.restore_policy != "prefetch":
                return False
            elapsed = max(0.0, time.time() - float(record.get("started_unix_s") or time.time()))
            return predicted_wait_s(record) - elapsed <= a.restore_prefetch_lead_s

        def evict_idle_candidate(record: dict) -> None:
            nonlocal snapshot_s, snapshots
            request_id = str(record["request_id"])
            try:
                with reclaim_accounting_lock:
                    if session_closing.is_set() or request_id in processed:
                        return
                    transition = checkpoint_coordinator.evict(lambda: {
                        "cgroup_memory_bytes": (
                            cgroup_value(tool_pool_cgroup, "memory.current")
                            if tool_pool_cgroup is not None else None
                        ),
                        "vm_cgroup_memory_bytes": (
                            cgroup_value(vm_pool_cgroup, "memory.current")
                            if vm_pool_cgroup is not None else None
                        ),
                        "numa_memory_bytes": numa_memory_used_bytes(numa_node),
                    })
                    if transition is None:
                        return
                    snapshot_s += transition.elapsed_s
                    runtime_released = max(
                        0,
                        transition.runtime_rss_before_bytes
                        - transition.runtime_rss_after_bytes,
                    )
                    tool_released = max(
                        0,
                        transition.tool_rss_before_bytes
                        - transition.tool_rss_after_bytes,
                    )
                    pair_released = runtime_released + tool_released
                    cgroup_before = transition.host_before["cgroup_memory_bytes"]
                    cgroup_after = transition.host_after["cgroup_memory_bytes"]
                    numa_before = transition.host_before["numa_memory_bytes"]
                    numa_after = transition.host_after["numa_memory_bytes"]
                    vm_cgroup_before = transition.host_before[
                        "vm_cgroup_memory_bytes"
                    ]
                    vm_cgroup_after = transition.host_after[
                        "vm_cgroup_memory_bytes"
                    ]
                    checkpoint_reclamation_events.append({
                        "request_id": request_id,
                        "reason": a.eviction_policy,
                        "victim_policy": "lru",
                        "idle_since_unix_s": float(record["started_unix_s"]),
                        "checkpoint_scope": transition.scope,
                        "predicted_llm_wait_s": predicted_wait_s(record),
                        "runtime_firecracker_rss_before_bytes": (
                            transition.runtime_rss_before_bytes
                        ),
                        "runtime_firecracker_rss_after_bytes": (
                            transition.runtime_rss_after_bytes
                        ),
                        "verified_runtime_firecracker_rss_released_bytes": (
                            runtime_released
                        ),
                        "tool_firecracker_rss_before_bytes": (
                            transition.tool_rss_before_bytes
                        ),
                        "tool_firecracker_rss_after_bytes": (
                            transition.tool_rss_after_bytes
                        ),
                        "verified_tool_firecracker_rss_released_bytes": tool_released,
                        "pair_firecracker_rss_before_bytes": (
                            transition.runtime_rss_before_bytes
                            + transition.tool_rss_before_bytes
                        ),
                        "pair_firecracker_rss_after_bytes": (
                            transition.runtime_rss_after_bytes
                            + transition.tool_rss_after_bytes
                        ),
                        "verified_pair_firecracker_rss_released_bytes": pair_released,
                        "cgroup_memory_before_bytes": cgroup_before,
                        "cgroup_memory_after_bytes": cgroup_after,
                        "cgroup_memory_released_bytes": (
                            None if cgroup_before is None or cgroup_after is None
                            else max(0, cgroup_before - cgroup_after)
                        ),
                        "vm_cgroup_memory_before_bytes": vm_cgroup_before,
                        "vm_cgroup_memory_after_bytes": vm_cgroup_after,
                        "vm_cgroup_memory_released_bytes": (
                            None
                            if vm_cgroup_before is None or vm_cgroup_after is None
                            else max(0, vm_cgroup_before - vm_cgroup_after)
                        ),
                        "numa_memory_before_bytes": numa_before,
                        "numa_memory_after_bytes": numa_after,
                        "numa_memory_released_bytes": (
                            None if numa_before is None or numa_after is None
                            else max(0, numa_before - numa_after)
                        ),
                        "snapshot_s": transition.elapsed_s,
                    })
                    processed.add(request_id)
                    snapshots += 1
                if (
                    pressure_reclaim_active.is_set()
                    and measure_tool_resident_bytes()
                    <= int(a.tool_pool_low_watermark_mib) * 1024 * 1024
                    and measure_vm_resident_bytes()
                    <= int(a.vm_pool_low_watermark_mib) * 1024 * 1024
                ):
                    pressure_reclaim_active.clear()
            except Exception as exc:
                idle_reclaim_errors.append(exc)

        gateway.start(spec["gateway_host"], 18081)
        deadline = time.monotonic() + a.timeout_s
        try:
            acquire_pair()
            def start_tool():
                elapsed = tool.start()
                wait_tcp(spec["tool_host"], 2222, 30)
                return elapsed
            with_memory_growth_admission(
                "initial_tool_boot",
                int(a.initial_tool_rss_reservation_mib) * 1024 * 1024,
                int(a.initial_tool_rss_reservation_mib) * 1024 * 1024,
                start_tool,
            )
            with_memory_growth_admission(
                "initial_runtime_boot",
                int(a.initial_runtime_rss_reservation_mib) * 1024 * 1024,
                0,
                runtime.start,
            )
            processed: set[str] = set()
            while time.monotonic() < deadline:
                finished, exit_code = complete(Path(runtime_config.log_path))
                if finished:
                    idle_sandboxes.unregister(index)
                    with reclaim_accounting_lock:
                        pass
                    if idle_reclaim_errors:
                        raise RuntimeError(
                            "idle sandbox reclamation failed"
                        ) from idle_reclaim_errors[0]
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
                    checkpoint_vm_operations = snapshots * (
                        2 if a.checkpoint_scope == "pair" else 1
                    )
                    return {"session": index, "snapshots": snapshots,
                            "checkpoint_cycles": snapshots,
                            "checkpoint_scope": a.checkpoint_scope,
                            "vm_snapshot_operations": checkpoint_vm_operations,
                            "vm_restore_operations": checkpoint_vm_operations,
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
                            "vm_materialization_events": vm_materialization_events,
                            "tool_working_sets": tool_working_sets,
                            "tool_working_set_artifact": str(working_set_path),
                            "validation_sha256": hashlib.sha256(validation).hexdigest(),
                            "validation_artifact": str(validation_path),
                            "model_requests": request_summaries(gateway)}
                runtime_exit = runtime.process_exit_code()
                tool_exit = tool.process_exit_code()
                if runtime_exit is not None:
                    raise RuntimeError(f"Runtime VM exited unexpectedly ({runtime_exit})")
                if tool.resident and tool_exit is not None:
                    raise RuntimeError(f"Tool VM exited unexpectedly ({tool_exit})")
                pending_all = [item for item in gateway.records() if not item["ready"]]
                pending = [
                    item for item in pending_all if item["request_id"] not in processed
                ]
                evicting = a.reclamation_policy in {"checkpoint", "hybrid"}
                if idle_reclaim_errors:
                    raise RuntimeError(
                        "idle sandbox reclamation failed"
                    ) from idle_reclaim_errors[0]
                if evicting and pending and idle_reclaim_complete.is_set():
                    record = pending[0]
                    idle_sandboxes.register(IdleSandboxCandidate(
                        session_id=index,
                        request_id=str(record["request_id"]),
                        idle_since_unix_s=float(
                            record.get("started_unix_s") or time.time()
                        ),
                        predicted_wait_s=predicted_wait_s(record),
                        evict=lambda record=record: evict_idle_candidate(record),
                    ))
                if (evicting and pending_all
                        and pending_all[0]["request_id"] in processed):
                    if prefetch_due(pending_all[0]):
                        restored = checkpoint_coordinator.restore(
                            restore_with_memory_admission
                        )
                        if restored is not None:
                            restore_s += float(restored["restore_s"])
                            checkpoint_reclamation_events.append({
                                "request_id": pending_all[0]["request_id"],
                                "reason": "prefetch_restore",
                                **restored,
                            })
                time.sleep(0.05)
            raise TimeoutError("OpenClaw experiment timed out")
        finally:
            session_closing.set()
            idle_sandboxes.unregister(index)
            with reclaim_accounting_lock:
                pass
            idle_reclaim_complete.wait(timeout=30.0)
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
        idle_sandboxes.close()
        victim_selector.join(timeout=5)
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
    if vm_admission is not None:
        cleanup_deadline = time.monotonic() + 2.0
        while vm_admission.metrics()["active_leases"] and time.monotonic() < cleanup_deadline:
            vm_admission.observe()
            time.sleep(0.01)
        leaked = vm_admission.metrics()["active_leases"]
        if leaked:
            failures.append({
                "session": None,
                "type": "VmAdmissionLeak",
                "error": f"{leaked} VM-pool admission leases remained after cleanup",
            })
    tool_pool_events_after = (
        cgroup_memory_events(tool_pool_cgroup) if tool_pool_cgroup is not None else {}
    )
    tool_pool_event_deltas = {
        key: int(value) - int(tool_pool_events_before.get(key, 0))
        for key, value in tool_pool_events_after.items()
    }
    vm_pool_events_after = (
        cgroup_memory_events(vm_pool_cgroup) if vm_pool_cgroup is not None else {}
    )
    vm_pool_event_deltas = {
        key: int(value) - int(vm_pool_events_before.get(key, 0))
        for key, value in vm_pool_events_after.items()
    }
    host_oom_kills = max(
        0,
        int(tool_pool_event_deltas.get("oom_kill", 0)),
        int(vm_pool_event_deltas.get("oom_kill", 0)),
    )
    tenant_guest_ooms = sum(
        guest_oom_observed(item.config.log_path) for item in tool_lifecycles
    )
    if host_oom_kills:
        failures.append({
            "session": None,
            "type": "OversubscriptionPolicyFailure",
            "error": (
                f"VM/Tool pool cgroups recorded {host_oom_kills} host OOM kill(s); "
                "this is an admission/reclaim policy failure"
            ),
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
    runtime_rss_values = [
        int(item["runtime_firecracker_rss_bytes"]) for item in samples
    ]
    tool_rss_values = [int(item["tool_firecracker_rss_bytes"]) for item in samples]
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
    vm_materialization_events = [
        event for item in results
        for event in item.get("vm_materialization_events", [])
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
        if row.get("verified_tool_firecracker_rss_released_bytes") is not None
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
    vm_admission_metrics = (
        vm_admission.metrics() if vm_admission is not None else None
    )
    report = {"mode": "snapshot" if a.reclamation_policy in {"checkpoint", "hybrid"} else "resident",
              "reclamation_policy": a.reclamation_policy,
              "residency_policy": a.residency_policy, "inference": a.inference,
              "numa_node": numa_node,
              "sessions_requested": len(raw["sessions"]), "sessions_completed": len(results),
              "configured_pair_memory_mib": pair_memory_mib,
              "resident_memory_budget_mib": a.resident_memory_budget_mib,
              "resident_pair_slots": resident_pair_slots,
              "cpu_pair_leasing": configured_cpu_pairs is not None,
              "cpu_pair_pool": configured_cpu_pairs,
              "tool_reservation_budget_mib": a.tool_reservation_budget_mib,
              "tool_reservation_policy": a.admission_policy,
              "admission_policy": a.admission_policy,
              "tool_vm_contract_mib": TOOL_VM_CONTRACT_MIB,
              "tool_pool_cgroup": str(tool_pool_cgroup) if tool_pool_cgroup else None,
              "tool_pool_hard_limit_mib": a.tool_pool_hard_limit_mib,
              "tool_pool_high_watermark_mib": a.tool_reservation_budget_mib,
              "tool_pool_low_watermark_mib": a.tool_pool_low_watermark_mib,
              "tool_pool_memory_events": tool_pool_event_deltas,
              "vm_pool_cgroup": str(vm_pool_cgroup) if vm_pool_cgroup else None,
              "runtime_pool_cgroup": (
                  str(runtime_pool_cgroup) if runtime_pool_cgroup else None
              ),
              "vm_pool_hard_limit_mib": a.vm_pool_hard_limit_mib,
              "vm_pool_high_watermark_mib": a.vm_pool_high_watermark_mib,
              "vm_pool_low_watermark_mib": a.vm_pool_low_watermark_mib,
              "vm_pool_headroom_mib": a.vm_pool_headroom_mib,
              "vm_pool_memory_events": vm_pool_event_deltas,
              "initial_runtime_rss_reservation_mib": (
                  a.initial_runtime_rss_reservation_mib
              ),
              "initial_tool_rss_reservation_mib": a.initial_tool_rss_reservation_mib,
              "restore_transient_headroom_mib": a.restore_transient_headroom_mib,
              "host_oom_kill_events": host_oom_kills,
              "oversubscription_policy_failures": host_oom_kills,
              "tenant_guest_oom_events": tenant_guest_ooms,
              "eviction_policy": (
                  a.eviction_policy
                  if a.reclamation_policy in {"checkpoint", "hybrid"} else None
              ),
              "restore_policy": (
                  a.restore_policy
                  if a.reclamation_policy in {"checkpoint", "hybrid"} else None
              ),
              "checkpoint_scope": (
                  a.checkpoint_scope
                  if a.reclamation_policy in {"checkpoint", "hybrid"} else None
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
              "vm_admission_feedback": vm_admission_metrics,
              "peak_vm_resident_rss_bytes": (
                  vm_admission_metrics["peak_resident_bytes"]
                  if vm_admission_metrics is not None else None
              ),
              "peak_vm_admission_charge_bytes": (
                  vm_admission_metrics["peak_admission_charge_bytes"]
                  if vm_admission_metrics is not None else None
              ),
              "vm_admission_over_budget_observations": (
                  vm_admission_metrics["over_budget_observations"]
                  if vm_admission_metrics is not None else None
              ),
              "vm_resident_plus_headroom_over_budget_observations": sum(
                  bool(sample.get("vm_resident_plus_headroom_over_budget"))
                  for sample in samples
              ) if vm_admission_metrics is not None else None,
              "tool_reservation_events": len(tool_reservation_events),
              "vm_materialization_admission_events": len(vm_materialization_events),
              "vm_materialization_admission_wait_s": sum(
                  float(event.get("wait_s") or 0.0)
                  for event in vm_materialization_events
              ),
              "max_vm_materialization_admission_wait_s": max(
                  (float(event.get("wait_s") or 0.0)
                   for event in vm_materialization_events),
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
              "model_gateway_http_attempts": sum(
                  int(item.get("http_attempts", 0)) for item in model_requests
              ),
              "model_gateway_reconnect_attempts": sum(
                  int(item.get("reconnect_attempts", 0)) for item in model_requests
              ),
              "model_gateway_delivery_failures": sum(
                  int(item.get("delivery_failures", 0)) for item in model_requests
              ),
              "model_gateway_responses_delivered": sum(
                  bool(item.get("delivered")) for item in model_requests
              ),
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
              "checkpoint_verified_runtime_rss_released_bytes": sum(
                  int(row["verified_runtime_firecracker_rss_released_bytes"])
                  for row in reclamation_events
              ),
              "checkpoint_verified_tool_rss_released_bytes": sum(
                  int(row["verified_tool_firecracker_rss_released_bytes"])
                  for row in reclamation_events
              ),
              "checkpoint_cgroup_memory_released_bytes": sum(
                  int(row["cgroup_memory_released_bytes"])
                  for row in reclamation_events
                  if row.get("cgroup_memory_released_bytes") is not None
              ),
              "checkpoint_vm_cgroup_memory_released_bytes": sum(
                  int(row["vm_cgroup_memory_released_bytes"])
                  for row in reclamation_events
                  if row.get("vm_cgroup_memory_released_bytes") is not None
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
              "mean_runtime_firecracker_rss_bytes": (
                  statistics.fmean(runtime_rss_values) if runtime_rss_values else 0
              ),
              "peak_runtime_firecracker_rss_bytes": max(runtime_rss_values, default=0),
              "mean_tool_firecracker_rss_bytes": (
                  statistics.fmean(tool_rss_values) if tool_rss_values else 0
              ),
              "peak_tool_firecracker_rss_bytes": max(tool_rss_values, default=0),
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
