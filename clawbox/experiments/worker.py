from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import time
import urllib.parse
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any

from clawbox.cube import (
    CubeCommandExecutor, CubeSandboxClient, CubeSandboxLifecycle,
    OwnedSandboxJournal, Ownership,
)
from clawbox.replay.trace import ReplayAction, load_trace

from .memory import NodeMemorySampler
from .clawtune_trace import ClawTuneTraceWriter
from .openclaw_driver import WorkerBridge, run_openclaw
from .policy import PolicyCoordinator
from .results import FailureCategory, ResultEnvelope, RunStatus, failure_category_for, utcnow
from .spec import (
    AdmissionPolicy, AgentDriver, EvictionPolicy, ExperimentArm, ExperimentSpec,
    ReclamationPolicy, RestorePolicy, expand_matrix, load_experiment,
)


def _network_target(endpoint: str, *, label: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} must be an http(s) URL with a hostname")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return parsed.hostname
    return f"{address}/{32 if address.version == 4 else 128}"


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def atomic_marker(path: Path, digest: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="ascii") as stream:
        stream.write(digest + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


class EventWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = Lock()

    def write(self, event: dict[str, Any]) -> None:
        row = {"wall_time": utcnow().isoformat(), **event}
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()


class ExperimentWorker:
    def __init__(self, spec: ExperimentSpec, *, run_id: str, attempt_id: str,
                 task_uid: str, output_root: Path | None = None,
                 client: CubeSandboxClient | None = None) -> None:
        self.spec = spec
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.task_uid = task_uid
        output_base = Path(os.environ.get("CLAWBOX_OUTPUT_ROOT", spec.output.directory))
        self.output_root = output_root or output_base / run_id
        journal = OwnedSandboxJournal(self.output_root / "owned-sandboxes.jsonl")
        self.client = client or CubeSandboxClient(journal=journal)
        if self.client.journal is None:
            self.client.journal = journal
        self.results: list[ResultEnvelope] = []
        self.worker_bridge: WorkerBridge | None = None

    def run(self) -> list[ResultEnvelope]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        bridge_host = os.environ.get("CLAWBOX_BRIDGE_HOST", "").strip()
        bridge_port = int(os.environ.get("CLAWBOX_BRIDGE_NODE_PORT", "0"))
        arms = list(expand_matrix(self.spec))
        requires_bridge = any(arm.agent.driver is AgentDriver.OPENCLAW for arm in arms)
        if requires_bridge and (not bridge_host or not bridge_port):
            raise RuntimeError("WorkerBridge requires CLAWBOX_BRIDGE_HOST and CLAWBOX_BRIDGE_NODE_PORT")
        bridge_context = (
            WorkerBridge(advertise_host=bridge_host, advertised_port=bridge_port)
            if bridge_host and bridge_port else nullcontext(None)
        )
        with bridge_context as bridge:
            self.worker_bridge = bridge
            if bridge is not None:
                bridge.wait_ready()
            for arm in arms:
                result_path = self.output_root / "arms" / f"{arm.arm_id}.json"
                marker_path = self.output_root / "arms" / f"{arm.arm_id}.complete"
                if self._completed(result_path, marker_path, arm.spec_digest):
                    self.results.append(ResultEnvelope.model_validate_json(result_path.read_text()))
                    continue
                self.results.append(self._run_arm(arm, result_path, marker_path))
            self.worker_bridge = None
        self._write_summary()
        return self.results

    @staticmethod
    def _completed(result_path: Path, marker_path: Path, digest: str) -> bool:
        if not result_path.exists() or not marker_path.exists():
            return False
        try:
            previous = json.loads(result_path.read_text(encoding="utf-8"))
            return marker_path.read_text(encoding="ascii").strip() == digest and \
                previous["arm"]["spec_digest"] == digest and \
                previous["status"] == RunStatus.SUCCEEDED
        except (OSError, ValueError, KeyError):
            return False

    def _run_arm(self, arm: ExperimentArm, result_path: Path, marker_path: Path) -> ResultEnvelope:
        started_at = utcnow()
        started = time.monotonic()
        events = EventWriter(self.output_root / "events" / f"{arm.arm_id}.jsonl")
        sampler = NodeMemorySampler(interval_s=arm.execution.memory_sample_interval_seconds)
        coordinator = PolicyCoordinator(
            arm.policy, budget_mib=arm.resources.pool_memory_budget_mib,
            emergency_free_mib=arm.resources.emergency_free_memory_mib,
            operation_headroom_mib=arm.resources.checkpoint_restore_headroom_mib,
            physical_sample=sampler.current,
        )
        sampler.start()
        sessions: list[dict[str, Any]] = []
        failure: Exception | None = None
        try:
            with ThreadPoolExecutor(max_workers=arm.concurrency, thread_name_prefix="agent") as pool:
                futures = [pool.submit(self._run_session, arm, index, coordinator, events)
                           for index in range(arm.concurrency)]
                for future in as_completed(futures, timeout=arm.execution.arm_timeout_seconds):
                    try:
                        sessions.append(future.result())
                    except Exception as exc:  # one failed session fails the whole arm
                        failure = failure or exc
            if failure is not None:
                raise failure
        except Exception as exc:
            failure = exc
            events.write({"event": "arm_failed", "error": str(exc), "type": type(exc).__name__})
        finally:
            # Isolation barrier: all session threads have ended, then kill and
            # verify every task-owned sandbox before the next arm can begin.
            cleanup_error = None
            try:
                self.client.kill_owned_sandboxes(self.task_uid)
            except Exception as exc:
                cleanup_error = exc
            time.sleep(arm.execution.stabilization_seconds)
            memory = sampler.stop()
            if cleanup_error is not None:
                failure = failure or cleanup_error
        status = RunStatus.SUCCEEDED if failure is None else RunStatus.FAILED
        duration = time.monotonic() - started
        session_durations = [float(item["duration_seconds"]) for item in sessions]
        tool_latencies = [float(value) for item in sessions for value in item["tool_latencies"]]
        tool_steps = sum(int(item["tool_steps"]) for item in sessions)
        model_steps = sum(int(item["model_steps"]) for item in sessions)
        result = ResultEnvelope(
            run_id=self.run_id, attempt_id=self.attempt_id,
            sandbox_task_uid=self.task_uid,
            experiment_id=self.spec.experiment_id, arm=arm,
            provenance=self._provenance(), status=status,
            failure_category=failure_category_for(status, "" if failure is None else str(failure)),
            started_at=started_at, completed_at=utcnow(),
            correctness={
                "completed_sessions": sum(item.get("valid", False) for item in sessions),
                "failed_sessions": arm.concurrency - sum(item.get("valid", False) for item in sessions),
                "validation_passed": failure is None and all(item.get("valid", False) for item in sessions),
                "failure": None if failure is None else str(failure),
                "session_output_hashes": [item.get("output_hash") for item in sessions],
            },
            performance={
                "duration_seconds": duration,
                "agents_per_minute": len(sessions) / max(duration, 1e-9) * 60,
                "steps_per_minute": (tool_steps + model_steps) / max(duration, 1e-9) * 60,
                "tool_steps": tool_steps, "model_steps": model_steps,
                "jct_mean_seconds": statistics.fmean(session_durations) if session_durations else None,
                "jct_p50_seconds": percentile(session_durations, 0.50),
                "jct_p90_seconds": percentile(session_durations, 0.90),
                "jct_p95_seconds": percentile(session_durations, 0.95),
                "tool_latency_mean_seconds": statistics.fmean(tool_latencies) if tool_latencies else None,
                "tool_latency_p50_seconds": percentile(tool_latencies, 0.50),
                "tool_latency_p90_seconds": percentile(tool_latencies, 0.90),
                "tool_latency_p95_seconds": percentile(tool_latencies, 0.95),
                "sandbox_create_mean_seconds": statistics.fmean(
                    [float(item["create_seconds"]) for item in sessions]) if sessions else None,
                "pause_count": coordinator.pause_count,
                "pause_service_seconds": coordinator.pause_service_seconds,
                "resume_count": coordinator.resume_count,
                "resume_service_seconds": coordinator.resume_service_seconds,
                "blocked_admission_seconds": coordinator.blocked_seconds,
            },
            memory={**asdict(memory), "peak_commitment_bytes": coordinator.peak_commitment_bytes},
            artifacts={"events": str(events.path)},
        )
        # Result first, complete marker second. A crash between them retries the
        # entire arm, never treating a partial result as complete.
        atomic_json(result_path, result.model_dump(mode="json"))
        atomic_marker(marker_path, arm.spec_digest)
        return result

    def _run_session(self, arm: ExperimentArm, index: int, coordinator: PolicyCoordinator,
                     events: EventWriter) -> dict[str, Any]:
        session_id = f"{arm.arm_id}-{index:04d}"
        session_started = time.monotonic()
        runtime_ownership = Ownership(
            self.run_id, self.attempt_id, self.task_uid, self.spec.experiment_id,
            f"{session_id}-runtime", arm.policy.name,
        )
        tool_ownership = Ownership(
            self.run_id, self.attempt_id, self.task_uid, self.spec.experiment_id,
            f"{session_id}-tool", arm.policy.name,
        )
        runtime_env: dict[str, str] = {}
        runtime_allow_out: list[str] = []
        if arm.agent.driver is AgentDriver.OPENCLAW:
            credential_name = str(arm.inference.configuration.get("api_key_env", "OPENCLAW_API_KEY"))
            credential = os.environ.get(credential_name)
            if not credential:
                raise ValueError(f"OpenClaw credential environment is missing: {credential_name}")
            runtime_env[credential_name] = credential
            runtime_env.update({
                "OPENAI_BASE_URL": str(arm.inference.configuration.get("base_url") or ""),
                "OPENCLAW_MODEL": str(arm.inference.configuration.get("model") or ""),
                "CLAWBOX_RUN_ID": self.run_id,
                "CLAWBOX_ATTEMPT_ID": self.attempt_id,
                "CLAWBOX_TENANT_ID": os.environ.get("CLAWBOX_TENANT_ID", "default"),
                "CLAWBOX_REPO_KEY": (
                    arm.case.repository
                    or str(arm.inference.configuration.get("repo_fingerprint") or arm.case.case_id)
                ),
            })
            for optional in ("CLAWBOX_KB_ENDPOINT", "CLAWBOX_KB_TOKEN"):
                if value := os.environ.get(optional):
                    runtime_env[optional] = value
            bridge_host = os.environ.get("CLAWBOX_BRIDGE_HOST", "").strip()
            try:
                bridge_address = ipaddress.ip_address(bridge_host)
            except ValueError as exc:
                raise ValueError(
                    "CLAWBOX_BRIDGE_HOST must be the Worker node InternalIP"
                ) from exc
            runtime_allow_out.append(
                f"{bridge_address}/{32 if bridge_address.version == 4 else 128}"
            )
            runtime_allow_out.append(_network_target(
                runtime_env["OPENAI_BASE_URL"], label="OpenClaw base_url",
            ))
            if kb_endpoint := runtime_env.get("CLAWBOX_KB_ENDPOINT"):
                runtime_allow_out.append(_network_target(
                    kb_endpoint, label="CLAWBOX_KB_ENDPOINT",
                ))
            runtime_allow_out = list(dict.fromkeys(runtime_allow_out))
        runtime_lifecycle = CubeSandboxLifecycle(
            self.client, template=arm.runtime.template,
            node_name=os.environ.get("CLAWBOX_WORKER_NODE", arm.resources.target_node),
            ownership=runtime_ownership,
            allow_internet_access=arm.runtime.allow_internet_access,
            env_vars=runtime_env,
            network_allow_out=runtime_allow_out,
            network_deny_out=["0.0.0.0/0"] if runtime_allow_out else None,
        )
        tool_env = {
            "TOOL_BRIDGE_LOG_PATH": "/var/lib/clawtune/artifacts/tool-bridge.jsonl",
            "TOOL_BRIDGE_WORKDIR": arm.sandbox.workspace,
            "TASK_ID": session_id,
            "CELL_ID": self.task_uid,
            "CLAWBOX_REPOSITORY": arm.case.repository or arm.case.case_id,
        }
        lifecycle = CubeSandboxLifecycle(
            self.client, template=arm.sandbox.template,
            node_name=os.environ.get("CLAWBOX_WORKER_NODE", arm.resources.target_node),
            ownership=tool_ownership, allow_internet_access=arm.sandbox.allow_internet_access,
            env_vars=tool_env,
        )
        coordinator.register(session_id, lifecycle)
        lifetime = (arm.runtime.memory_mib + arm.sandbox.memory_mib
                    if arm.policy.admission is AdmissionPolicy.LIFETIME_FULL else 0)
        try:
            if lifetime:
                coordinator.acquire(session_id, lifetime, arm.execution.arm_timeout_seconds)
            runtime_create_s = runtime_lifecycle.start()
            events.write({"event": "sandbox_created", "session_id": session_id,
                          "role": "runtime", "service_seconds": runtime_create_s,
                          "sandbox_id": self.client.sandbox_id(runtime_lifecycle.sandbox)})
            tool_create_s = lifecycle.start()
            events.write({"event": "sandbox_created", "session_id": session_id,
                          "role": "tool", "service_seconds": tool_create_s,
                          "sandbox_id": self.client.sandbox_id(lifecycle.sandbox)})
            create_s = runtime_create_s + tool_create_s
            runtime_executor = CubeCommandExecutor(
                self.client, lambda: runtime_lifecycle.sandbox, cwd=arm.runtime.workspace,
            )
            executor = CubeCommandExecutor(self.client, lambda: lifecycle.sandbox, cwd=arm.sandbox.workspace)
            exit_mismatches = 0
            model_steps = 0
            tool_latencies: list[float] = []
            trace_writer = ClawTuneTraceWriter(
                self.output_root, run_id=self.run_id, session_id=session_id,
                repo_fingerprint=(arm.case.repository or str(
                    arm.inference.configuration.get("repo_fingerprint") or ""
                ) or None),
            )

            def execute_observed(command: str, timeout_s: float,
                                 execution_id: str):
                observed = executor.execute_observed(
                    command, min(timeout_s, arm.execution.command_timeout_seconds),
                    execution_id=execution_id,
                )
                trace_writer.record(
                    command, observed.result, execution_id=execution_id,
                    bridge_record=observed.bridge_record, artifacts=observed.artifacts,
                )
                events.write({
                    "event": "clawtune_observation", "session_id": session_id,
                    "execution_id": execution_id, "tool": "cube_shell",
                    "telemetry_state": observed.bridge_record.get("telemetry_state"),
                    "telemetry_unavailable_reason": observed.telemetry_unavailable_reason,
                    "artifact_kinds": sorted(observed.artifacts),
                })
                return observed.result

            if arm.agent.driver is AgentDriver.OPENCLAW:
                def execute_openclaw_tool(command: str, timeout_s: float,
                                          execution_id: str):
                    if not lifecycle.resident:
                        self._restore_with_one_victim(
                            arm, session_id, lifecycle, coordinator, events,
                        )
                    action = SimpleNamespace(action_id="cube_shell")
                    amount = self._tool_reservation_mib(arm, action)
                    coordinator.acquire(session_id, amount, arm.execution.arm_timeout_seconds)
                    coordinator.set_tool_active(session_id, True)
                    try:
                        result = execute_observed(command, timeout_s, execution_id)
                    finally:
                        coordinator.set_tool_active(session_id, False)
                        coordinator.release(session_id, amount)
                    if (arm.policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE
                            and arm.policy.eviction is EvictionPolicy.EAGER):
                        pause_s = lifecycle.checkpoint_and_evict()
                        coordinator.pause_count += 1
                        coordinator.pause_service_seconds += pause_s
                        events.write({"event": "sandbox_paused", "session_id": session_id,
                                      "service_seconds": pause_s, "reason": "openclaw_tool_complete"})
                    return result

                if self.worker_bridge is None:
                    raise RuntimeError("WorkerBridge is not active")
                bridge = self.worker_bridge.register(
                    session_id, execute_openclaw_tool, task_id=self.task_uid,
                    tool_sandbox_id=self.client.sandbox_id(lifecycle.sandbox),
                )
                try:
                    outcome = run_openclaw(
                        prompt=arm.case.prompt, session_id=session_id,
                        configuration=arm.inference.configuration, bridge=bridge,
                        runtime_executor=runtime_executor,
                        output_dir=self.output_root,
                        timeout_seconds=arm.execution.arm_timeout_seconds,
                    )
                finally:
                    bridge.close()
                tool_latencies.extend(float(item) for item in outcome["tool_latencies"])
                model_steps = 1
                if not tool_latencies:
                    raise RuntimeError("OpenClaw completed without using the required cube_shell tool")
            else:
                actions = self._actions(arm)
                for action_index, action in enumerate(actions):
                    if action.kind == "llm":
                        model_steps += 1
                        self._model_wait(arm, action, session_id, lifecycle, coordinator, events)
                        continue
                    if not lifecycle.resident:
                        self._restore_with_one_victim(arm, session_id, lifecycle, coordinator, events)
                    amount = self._tool_reservation_mib(arm, action)
                    coordinator.acquire(session_id, amount, arm.execution.arm_timeout_seconds)
                    coordinator.set_tool_active(session_id, True)
                    try:
                        result = execute_observed(
                            action.shell_command(), arm.execution.command_timeout_seconds,
                            f"{session_id}:replay:{action_index}:"
                            f"{hashlib.sha256(action.action_id.encode()).hexdigest()[:16]}",
                        )
                        tool_latencies.append(result.duration_s)
                        if action.expected_exit_code is not None and result.exit_code != action.expected_exit_code:
                            exit_mismatches += 1
                    finally:
                        coordinator.set_tool_active(session_id, False)
                        coordinator.release(session_id, amount)
            if not lifecycle.resident:
                self._restore_with_one_victim(arm, session_id, lifecycle, coordinator, events)
            validation = arm.validation.command or (
                arm.case.validation if isinstance(arm.case.validation, str) else None)
            valid = exit_mismatches == 0
            if validation:
                validation_result = execute_observed(
                    validation, arm.execution.command_timeout_seconds,
                    f"{session_id}:validation",
                )
                valid = valid and validation_result.exit_code == 0
            hash_result = execute_observed(
                "find . -type f -exec sha256sum {} \\; | LC_ALL=C sort | sha256sum",
                arm.execution.command_timeout_seconds,
                f"{session_id}:output-hash",
            )
            output_hash = (hash_result.stdout.split()[0] if hash_result.exit_code == 0
                           and hash_result.stdout.split() else hashlib.sha256(
                               json.dumps({"valid": valid, "mismatches": exit_mismatches},
                                          sort_keys=True).encode()).hexdigest())
            events.write({"event": "session_complete", "session_id": session_id, "valid": valid})
            if not valid:
                raise RuntimeError(f"validation failed for {session_id}")
            return {"session_id": session_id, "valid": valid, "create_seconds": create_s,
                    "runtime_create_seconds": runtime_create_s,
                    "tool_create_seconds": tool_create_s,
                    "duration_seconds": time.monotonic() - session_started,
                    "tool_steps": len(tool_latencies), "model_steps": model_steps,
                    "tool_latencies": tool_latencies, "output_hash": output_hash}
        finally:
            try:
                lifecycle.close()
            finally:
                try:
                    runtime_lifecycle.close()
                finally:
                    if lifetime:
                        coordinator.release(session_id, lifetime)
                    coordinator.unregister(session_id)

    def _actions(self, arm: ExperimentArm) -> list[ReplayAction]:
        if arm.agent.driver is not AgentDriver.REPLAY_ENGINE:
            raise RuntimeError(f"unsupported agent driver: {arm.agent.driver}")
        trace = arm.case.replay_trace_reference or arm.case.source_reference
        return load_trace(Path(trace))

    def _model_wait(self, arm: ExperimentArm, action: ReplayAction, session_id: str,
                    lifecycle: CubeSandboxLifecycle, coordinator: PolicyCoordinator,
                    events: EventWriter) -> None:
        scale = float(arm.inference.configuration.get("time_scale", 1.0))
        duration = max(0.0, action.duration_s * scale)
        coordinator.set_eviction_eligible(session_id, True)
        delay, prefetch_lead = coordinator.model_wait_plan(duration)
        should_pause = delay is not None
        wait_started = time.monotonic()
        if should_pause:
            time.sleep(min(delay, duration))
            pause_s = lifecycle.checkpoint_and_evict()
            coordinator.pause_count += 1
            coordinator.pause_service_seconds += pause_s
            events.write({"event": "sandbox_paused", "session_id": session_id,
                          "service_seconds": pause_s})
            remaining = max(0.0, duration - (time.monotonic() - wait_started))
            if prefetch_lead is not None:
                time.sleep(max(0.0, remaining - prefetch_lead))
                self._restore_with_one_victim(arm, session_id, lifecycle, coordinator, events)
            else:
                time.sleep(remaining)
        else:
            time.sleep(duration)
        coordinator.set_eviction_eligible(session_id, not lifecycle.resident)

    def _restore_with_one_victim(self, arm: ExperimentArm, session_id: str,
                                 lifecycle: CubeSandboxLifecycle,
                                 coordinator: PolicyCoordinator, events: EventWriter) -> None:
        elapsed = coordinator.restore(
            session_id, lifecycle.restore, arm.execution.arm_timeout_seconds,
        )
        events.write({"event": "sandbox_restored", "session_id": session_id,
                      "service_seconds": elapsed})

    def _tool_reservation_mib(self, arm: ExperimentArm, action: ReplayAction) -> int:
        policy = arm.policy.admission
        if policy is AdmissionPolicy.LIFETIME_FULL:
            return 0
        if policy is AdmissionPolicy.TOOL_FULL:
            return int(arm.resources.full_tool_memory_mib or arm.sandbox.memory_mib)
        if policy is AdmissionPolicy.TOOL_STATIC:
            return int(arm.resources.static_tool_memory_mib or 1)
        source = arm.resources.p90_predictions if policy is AdmissionPolicy.TOOL_P90 else arm.resources.oracle_measurements
        payload = json.loads(Path(str(source)).read_text(encoding="utf-8"))
        return max(1, int(payload.get(action.action_id, payload.get("default_incremental_mib", 1))))

    def _provenance(self) -> dict[str, Any]:
        def command(*args: str) -> str:
            try:
                return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()
            except (OSError, subprocess.CalledProcessError):
                return "unknown"
        try:
            sdk = importlib.metadata.version("cubesandbox")
        except importlib.metadata.PackageNotFoundError:
            sdk = "unavailable"
        return {
            "cubesandbox_tag": "v0.7.0",
            "cubesandbox_commit": "d0081641c59822e4e5653b7462e914410b81910a",
            "cubesandbox_sdk": sdk,
            "clawbox_commit": os.environ.get("CLAWBOX_REVISION", command("git", "rev-parse", "HEAD")),
            "kubernetes_version": os.environ.get("CLAWBOX_KUBERNETES_VERSION", "unknown"),
            "containerd_version": os.environ.get("CLAWBOX_CONTAINERD_VERSION", "unknown"),
            "os_release": platform.platform(), "kernel": platform.release(),
            "architecture": platform.machine(), "cpu_model": platform.processor(),
            "template_reference": self.spec.sandbox.template,
            "template_source_image": self.spec.sandbox.source_image_reference or "unknown",
            "template_image_digest": self.spec.sandbox.image_digest or os.environ.get("CLAWBOX_TEMPLATE_IMAGE_DIGEST", "unknown"),
            "measurement_scope": "node-level physical memory, baseline-subtracted per arm",
            "cube_runtime_settings": {
                "cpu_overcommit_ratio": 1.0, "memory_overcommit_ratio": 1.0,
                "paused_resource_release_ratio": 1.0,
            },
        }

    def _write_summary(self) -> None:
        summary = {"run_id": self.run_id, "attempt_id": self.attempt_id,
                   "experiment_id": self.spec.experiment_id,
                   "arms": [item.model_dump(mode="json") for item in self.results]}
        atomic_json(self.output_root / "summary.json", summary)
        path = self.output_root / "summary.csv"
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["arm_id", "policy", "concurrency",
                                                        "repetition", "status", "duration_seconds"])
            writer.writeheader()
            for item in self.results:
                writer.writerow({"arm_id": item.arm.arm_id, "policy": item.arm.policy.name,
                                 "concurrency": item.arm.concurrency, "repetition": item.arm.repetition,
                                 "status": item.status, "duration_seconds": item.performance.get("duration_seconds")})
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        lines = [
            f"# Experiment {self.spec.experiment_id}", "",
            f"Run: `{self.run_id}`  ",
            f"Attempt: `{self.attempt_id}`  ",
            f"SandboxTask UID: `{self.task_uid}`", "",
            "| Arm | Policy | Agents | Status | Duration (s) | Pauses | Resumes |",
            "|---|---|---:|---|---:|---:|---:|",
        ]
        for item in self.results:
            lines.append(
                f"| `{item.arm.arm_id}` | {item.arm.policy.name} | {item.arm.concurrency} | "
                f"{item.status.value} | {item.performance.get('duration_seconds', 0):.3f} | "
                f"{item.performance.get('pause_count', 0)} | {item.performance.get('resume_count', 0)} |"
            )
        markdown = self.output_root / "summary.md"
        markdown_tmp = markdown.with_name(markdown.name + ".tmp")
        markdown_tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(markdown_tmp, markdown)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one ClawBox experiment attempt")
    parser.add_argument("--spec", type=Path, default=Path(os.environ.get("CLAWBOX_EXPERIMENT_SPEC", "/config/experiment.yaml")))
    parser.add_argument("--run-id", default=os.environ.get("CLAWBOX_RUN_ID"), required=os.environ.get("CLAWBOX_RUN_ID") is None)
    parser.add_argument("--attempt-id", default=os.environ.get("CLAWBOX_ATTEMPT_ID"), required=os.environ.get("CLAWBOX_ATTEMPT_ID") is None)
    parser.add_argument("--task-uid", default=os.environ.get("CLAWBOX_TASK_UID"), required=os.environ.get("CLAWBOX_TASK_UID") is None)
    args = parser.parse_args(argv)
    results = ExperimentWorker(load_experiment(args.spec), run_id=args.run_id,
                               attempt_id=args.attempt_id, task_uid=args.task_uid).run()
    return 0 if all(result.status is RunStatus.SUCCEEDED for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
