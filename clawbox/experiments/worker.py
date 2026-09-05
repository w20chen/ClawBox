from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import importlib.metadata
import json
import math
import os
import platform
import statistics
import subprocess
import base64
import sys
import time
import traceback
import urllib.parse
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path
from threading import Lock, Timer
from types import SimpleNamespace
from typing import Any

from clawbox.cube import (
    CubeCommandExecutor, CubeSandboxClient, CubeSandboxLifecycle,
    OwnedSandboxJournal, Ownership,
)
from clawbox.replay.trace import ReplayAction, load_trace

from .memory import NodeMemorySampler
from .clawtune_trace import ClawTuneTraceWriter
from .model_gateway import ManagedModelGateway, SessionGatewayState
from .native_artifacts import collect_and_validate_native_tool_artifacts
from .openclaw_driver import (
    NativeSSHConfig, NativeSSHRouteState, RUNTIME_LOCAL_TOOLS, TOOL_VM_TOOLS,
    native_ssh_target,
    native_tool_bridge_setup_command, run_openclaw, split_native_ssh_target,
)
from .policy import PolicyCoordinator, PolicyEventExecutor
from .policy_control import PolicyControlServer
from .prediction import CommandPredictionProvider, PredictionUnavailable
from .ssh_credentials import generate_ssh_credentials
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


def build_time_spans(timeline: dict[str, Any]) -> list[dict[str, float | str]]:
    """Build stable, machine-readable spans from session lifecycle markers."""
    pairs = (
        ("session", "session_started", "session_finished"),
        ("sandbox.create", "sandbox_create_start", "sandbox_ready"),
        ("sandbox.tool.create", "tool_create_start", "tool_ready"),
        ("sandbox.runtime.create", "runtime_create_start", "runtime_ready"),
        ("agent", "agent_execution_start", "final_agent_completion"),
        ("validation", "validation_start", "validation_end"),
        ("output_hash", "output_hash_start", "output_hash_end"),
        ("sandbox.cleanup", "sandbox_cleanup_start", "sandbox_cleanup_end"),
    )
    spans: list[dict[str, float | str]] = []
    for name, start_key, end_key in pairs:
        start = timeline.get(start_key)
        end = timeline.get(end_key)
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        spans.append({
            "name": name,
            "start_unix_s": float(start),
            "end_unix_s": float(end),
            "duration_seconds": max(0.0, float(end) - float(start)),
        })
    return spans


class EventWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = Lock()
        self.sequence = 0

    def write(self, event: dict[str, Any]) -> None:
        with self.lock, self.path.open("a", encoding="utf-8") as stream:
            row = {
                "schema_version": 1,
                "sequence_no": self.sequence,
                "wall_time": utcnow().isoformat(),
                "wall_time_ns": str(time.time_ns()),
                "monotonic_time_ns": str(time.monotonic_ns()),
                **event,
            }
            self.sequence += 1
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
        self.policy_control: PolicyControlServer | None = None
        self.model_gateway: ManagedModelGateway | None = None

    def run(self) -> list[ResultEnvelope]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        control_host = os.environ.get("CLAWBOX_CONTROL_HOST", "").strip()
        policy_port = int(os.environ.get("CLAWBOX_POLICY_PORT", "18080"))
        gateway_host = os.environ.get("CLAWBOX_MODEL_GATEWAY_HOST", control_host).strip()
        gateway_port = int(os.environ.get("CLAWBOX_MODEL_GATEWAY_PORT", "18081"))
        arms = list(expand_matrix(self.spec))
        requires_control = any(arm.agent.driver is AgentDriver.OPENCLAW for arm in arms)
        if requires_control and not control_host:
            raise RuntimeError("native SSH policy control requires CLAWBOX_CONTROL_HOST")
        if requires_control and not gateway_host:
            raise RuntimeError(
                "managed ModelGateway requires CLAWBOX_MODEL_GATEWAY_HOST and "
                "CLAWBOX_MODEL_GATEWAY_NODE_PORT"
            )
        policy_context = (
            PolicyControlServer(advertise_host=control_host, advertised_port=policy_port,
                                bind_port=policy_port)
            if control_host else nullcontext(None)
        )
        gateway_context = (
            ManagedModelGateway(advertise_host=gateway_host, advertised_port=gateway_port)
            if gateway_host and gateway_port else nullcontext(None)
        )
        with policy_context as policy_control, gateway_context as model_gateway:
            self.policy_control = policy_control
            self.model_gateway = model_gateway
            if model_gateway is not None:
                model_gateway.wait_ready()
            for arm in arms:
                result_path = self.output_root / "arms" / f"{arm.arm_id}.json"
                marker_path = self.output_root / "arms" / f"{arm.arm_id}.complete"
                if self._completed(result_path, marker_path, arm.spec_digest):
                    self.results.append(ResultEnvelope.model_validate_json(result_path.read_text()))
                    continue
                self.results.append(self._run_arm(arm, result_path, marker_path))
            self.policy_control = None
            self.model_gateway = None
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
        prediction_provider = None
        if arm.agent.driver is AgentDriver.OPENCLAW and arm.policy.admission is AdmissionPolicy.TOOL_P90:
            if not arm.resources.p90_predictions:
                raise ValueError("tool_p90 requires an immutable command prediction artifact")
            prediction_provider = CommandPredictionProvider(
                Path(arm.resources.p90_predictions),
                repository=arm.case.repository or arm.case.case_id,
            )
        policy_events = PolicyEventExecutor(workers=max(4, arm.concurrency * 2))
        sampler.start()
        sessions: list[dict[str, Any]] = []
        failure: Exception | None = None
        try:
            with ThreadPoolExecutor(max_workers=arm.concurrency, thread_name_prefix="agent") as pool:
                futures = [pool.submit(self._run_session, arm, index, coordinator, events,
                                       policy_events, prediction_provider)
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
            policy_events.close()
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
        session_durations = [float(item.get("agent_jct_seconds", item["duration_seconds"]))
                             for item in sessions]
        tool_latencies = [float(value) for item in sessions for value in item["tool_latencies"]]
        admission_round_trips = [
            float(value) for item in sessions
            for value in item.get("admission_round_trip_seconds", [])
        ]
        admission_overheads = [
            float(value) for item in sessions
            for value in item.get("admission_control_overhead_seconds", [])
        ]
        tool_steps = sum(int(item["tool_steps"]) for item in sessions)
        model_steps = sum(int(item["model_steps"]) for item in sessions)
        workload_starts = [float(item["timeline"]["agent_execution_start"])
                           for item in sessions if item.get("timeline", {}).get("agent_execution_start")]
        workload_ends = [float(item["timeline"]["final_agent_completion"])
                         for item in sessions if item.get("timeline", {}).get("final_agent_completion")]
        workload_window = (max(workload_ends) - min(workload_starts)
                           if workload_starts and workload_ends else None)
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
                "native_tool_exact_id_join_rate": (
                    min(
                        float((item.get("native_tool_artifact_validation") or {})
                             .get("exact_id_join_rate", 0.0))
                        for item in sessions
                        if item.get("native_tool_artifact_validation") is not None
                    )
                    if any(item.get("native_tool_artifact_validation") is not None for item in sessions)
                    else None
                ),
                "native_tool_telemetry_loss_total": sum(
                    int((item.get("native_tool_artifact_validation") or {})
                        .get("telemetry_loss_total", 0))
                    for item in sessions
                ),
                "failure": None if failure is None else str(failure),
                "session_output_hashes": [item.get("output_hash") for item in sessions],
            },
            performance={
                "duration_seconds": duration,
                "infrastructure_inclusive_duration_seconds": duration,
                "workload_window_seconds": workload_window,
                "agents_per_minute": len(sessions) / max(workload_window or duration, 1e-9) * 60,
                "steps_per_minute": (tool_steps + model_steps) / max(workload_window or duration, 1e-9) * 60,
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
                "admission_control": coordinator.admission_metrics(),
                "admission_round_trip_mean_seconds": (
                    statistics.fmean(admission_round_trips) if admission_round_trips else None
                ),
                "admission_round_trip_p95_seconds": percentile(admission_round_trips, 0.95),
                "admission_control_overhead_mean_seconds": (
                    statistics.fmean(admission_overheads) if admission_overheads else None
                ),
                "admission_control_overhead_p95_seconds": percentile(
                    admission_overheads, 0.95,
                ),
                "session_timelines": [item.get("timeline") for item in sessions],
                "session_time_spans": [
                    item.get("timeline", {}).get("time_spans", []) for item in sessions
                ],
            },
            memory={**asdict(memory), "peak_commitment_bytes": coordinator.peak_commitment_bytes},
            artifacts={
                "events": str(events.path),
                **{
                    f"native_tool_artifacts:{item['session_id']}": str(item["native_tool_artifact_root"])
                    for item in sessions
                    if item.get("native_tool_artifact_root")
                },
                **{
                    f"policy_control:{item['session_id']}": str(item["policy_control_path"])
                    for item in sessions
                    if item.get("policy_control_path")
                },
                **{
                    f"runtime_traces:{item['session_id']}": str(item["runtime_trace_root"])
                    for item in sessions
                    if item.get("runtime_trace_root")
                },
            },
        )
        # Result first, complete marker second. A crash between them retries the
        # entire arm, never treating a partial result as complete.
        atomic_json(result_path, result.model_dump(mode="json"))
        atomic_marker(marker_path, arm.spec_digest)
        return result

    def _run_session(self, arm: ExperimentArm, index: int, coordinator: PolicyCoordinator,
                     events: EventWriter, policy_events: PolicyEventExecutor,
                     prediction_provider: CommandPredictionProvider | None = None) -> dict[str, Any]:
        session_id = f"{arm.arm_id}-{index:04d}"
        session_started = time.monotonic()
        timeline: dict[str, Any] = {"session_started": time.time()}
        events.write({
            "event": "session_started",
            "session_id": session_id,
            "agent_driver": arm.agent.driver.value,
            "inference_backend": arm.inference.backend.value,
            "formal_openclaw_path": arm.agent.driver is AgentDriver.OPENCLAW,
            "tool_vm_tools": (
                list(TOOL_VM_TOOLS) if arm.agent.driver is AgentDriver.OPENCLAW else []
            ),
            "runtime_local_tools": (
                list(RUNTIME_LOCAL_TOOLS) if arm.agent.driver is AgentDriver.OPENCLAW else []
            ),
        })
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
        route_state: NativeSSHRouteState | None = None
        ssh_config: NativeSSHConfig | None = None
        native_route_host: str | None = None
        if arm.agent.driver is AgentDriver.OPENCLAW:
            credential_name = str(arm.inference.configuration.get("api_key_env", "OPENCLAW_API_KEY"))
            credential = os.environ.get(credential_name, "")
            if arm.inference.backend.value == "api" and not credential:
                raise ValueError(f"OpenClaw credential environment is missing: {credential_name}")
            runtime_env.update({
                # Runtime talks only to the Worker-owned node-routed gateway;
                # the upstream API key stays in the Worker process.
                "OPENAI_BASE_URL": "",
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
            control_host = os.environ.get("CLAWBOX_CONTROL_HOST", "").strip()
            try:
                control_address = ipaddress.ip_address(control_host)
            except ValueError as exc:
                raise ValueError(
                    "CLAWBOX_CONTROL_HOST must be a host IP reachable from Runtime VMs"
                ) from exc
            runtime_allow_out.append(
                f"{control_address}/{32 if control_address.version == 4 else 128}"
            )
            # ModelGateway and PolicyControl share the host address and have
            # distinct direct listeners; Kubernetes/NodePort is not involved.
            if self.model_gateway is None:
                raise RuntimeError("managed ModelGateway is not active")
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
            # Runtime-local web and memory tools remain usable when the
            # experiment explicitly permits Internet access. Closed-network
            # arms allow only PolicyControl, ModelGateway, KB, and Tool SSH.
            network_deny_out=(
                ["0.0.0.0/0"]
                if runtime_allow_out and not arm.runtime.allow_internet_access else None
            ),
        )
        ssh_credentials = generate_ssh_credentials()
        tool_env = {
            "TOOL_BRIDGE_LOG_PATH": "/var/lib/clawtune/artifacts/tool-bridge.jsonl",
            "TOOL_BRIDGE_WORKDIR": arm.sandbox.workspace,
            "TOOL_MAX_CONCURRENCY": "1",
            "CLAWBOX_TOOL_HOST_KEY_B64": base64.b64encode(
                ssh_credentials.host_private.encode()
            ).decode(),
            "CLAWBOX_TOOL_AUTHORIZED_KEY_B64": base64.b64encode(
                (ssh_credentials.client_public + "\n").encode()
            ).decode(),
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
        lifetime = (arm.runtime.memory_mib + arm.sandbox.memory_mib
                    if arm.policy.admission is AdmissionPolicy.LIFETIME_FULL else 0)
        gateway_session: SessionGatewayState | None = None
        policy_session = None
        policy_drained = True
        wait_lock = Lock()
        wait_timer: Timer | None = None
        restore_timer: Timer | None = None
        wait_state: dict[str, Any] = {}
        completed_model_requests: set[str] = set()
        policy_event_errors: list[str] = []

        if arm.agent.driver is AgentDriver.OPENCLAW:
            if self.model_gateway is None:
                raise RuntimeError("managed ModelGateway is not active")
            prediction_wait = arm.inference.configuration.get("model_wait_prediction_seconds")
            if arm.policy.restore is RestorePolicy.PROACTIVE:
                if prediction_wait is None:
                    raise ValueError(
                        "proactive restore requires explicit model_wait_prediction_seconds"
                    )
                try:
                    prediction_wait = float(prediction_wait)
                except (TypeError, ValueError) as exc:
                    raise ValueError("model_wait_prediction_seconds must be finite") from exc
                if prediction_wait < 0:
                    raise ValueError("model_wait_prediction_seconds must be non-negative")
            else:
                prediction_wait = None

            def pause_for_model_wait(event: dict[str, Any]) -> None:
                nonlocal wait_timer
                try:
                    with wait_lock:
                        wait_timer = None
                        if not lifecycle.resident or coordinator.tool_active(session_id):
                            return
                        started = time.time()
                        elapsed = lifecycle.checkpoint_and_evict()
                        finished = time.time()
                        wait_state.update({
                            "pause_started_at": started,
                            "pause_completed_at": finished,
                            "pause_request_id": event.get("request_id"),
                        })
                    coordinator.pause_count += 1
                    coordinator.pause_service_seconds += elapsed
                    events.write({
                        "event": "sandbox_paused", "session_id": session_id,
                        "service_seconds": elapsed,
                        "reason": "model_request_wait",
                        "request_id": event.get("request_id"),
                    })
                except Exception as exc:
                    policy_event_errors.append(f"pause: {type(exc).__name__}: {exc}")

            def restore_for_model_wait(event: dict[str, Any]) -> None:
                nonlocal restore_timer
                try:
                    with wait_lock:
                        restore_timer = None
                        if str(event.get("request_id")) in completed_model_requests:
                            return
                        if lifecycle.resident:
                            target = wait_state.get("scheduled_restore_time")
                            remaining = max(
                                0.05,
                                float(target) - time.time() if target is not None else 0.05,
                            )
                            restore_timer = Timer(
                                remaining,
                                lambda: policy_events.submit(restore_for_model_wait, event),
                            )
                            restore_timer.daemon = True
                            restore_timer.start()
                            return
                        wait_state["restore_started_at"] = time.time()
                    elapsed = self._restore_with_one_victim(
                        arm, session_id, lifecycle, coordinator, events,
                    )
                    refresh_native_ssh_route()
                    with wait_lock:
                        wait_state["restore_completed_at"] = time.time()
                        wait_state["restore_request_id"] = event.get("request_id")
                    # _restore_with_one_victim already records the service
                    # event; retain the explicit timing for gateway provenance.
                    _ = elapsed
                except Exception as exc:
                    policy_event_errors.append(f"restore: {type(exc).__name__}: {exc}")

            def on_model_request_started(event: dict[str, Any]) -> None:
                """Enqueue lifecycle work; never checkpoint in the gateway callback."""
                policy_events.submit(handle_model_request_started, event)

            def handle_model_request_started(event: dict[str, Any]) -> None:
                nonlocal wait_timer, restore_timer
                timeline.setdefault("first_model_request", float(event["request_started_at"]))
                with wait_lock:
                    # A zero-scaled replay response can arrive before the
                    # executor gets scheduled. Never create a late checkpoint
                    # for an already completed model request.
                    if str(event.get("request_id")) in completed_model_requests:
                        return
                coordinator.set_eviction_eligible(session_id, True)
                if arm.policy.reclamation is ReclamationPolicy.RESIDENT:
                    return
                if arm.policy.eviction is EvictionPolicy.EAGER:
                    delay = 0.0
                elif arm.policy.eviction is EvictionPolicy.FIXED_DELAY:
                    delay = float(arm.policy.fixed_delay_seconds or 0.0)
                elif arm.policy.eviction is EvictionPolicy.WAIT_AWARE_PRESSURE:
                    delay = 0.0 if coordinator.pressure() else None
                else:
                    delay = None
                with wait_lock:
                    wait_state.clear()
                    wait_state.update({
                        "request_id": event.get("request_id"),
                        "request_started_at": event.get("request_started_at"),
                        "predicted_wait_seconds": prediction_wait,
                        "prediction_source": (
                            "configured_request_time_prediction"
                            if prediction_wait is not None else None
                        ),
                    })
                    elapsed_since_request = max(
                        0.0, time.time() - float(event["request_started_at"])
                    )
                    restore_target = None
                    if prediction_wait is not None and arm.policy.restore is RestorePolicy.PROACTIVE:
                        restore_target = (
                            float(event["request_started_at"])
                            + prediction_wait
                            - float(arm.policy.prefetch_lead_seconds or 0.0)
                        )
                        wait_state["scheduled_restore_time"] = restore_target
                    if delay is not None:
                        wait_timer = Timer(
                            max(0.0, float(delay) - elapsed_since_request),
                            lambda: policy_events.submit(pause_for_model_wait, event),
                        )
                        wait_timer.daemon = True
                        wait_timer.start()
                    if prediction_wait is not None and arm.policy.restore is RestorePolicy.PROACTIVE:
                        lead = float(arm.policy.prefetch_lead_seconds or 0.0)
                        restore_timer = Timer(
                            max(0.0, prediction_wait - lead - elapsed_since_request),
                            lambda: policy_events.submit(restore_for_model_wait, event),
                        )
                        restore_timer.daemon = True
                        restore_timer.start()

            def before_model_response_ready(step: int | None, message: dict[str, Any],
                                            event: dict[str, Any]) -> dict[str, Any]:
                nonlocal wait_timer, restore_timer
                with wait_lock:
                    completed_model_requests.add(str(event.get("request_id")))
                    if wait_timer is not None:
                        wait_timer.cancel()
                        wait_timer = None
                    if restore_timer is not None:
                        restore_timer.cancel()
                        restore_timer = None
                    started = float(event["request_started_at"])
                    generated = float(event["model_generated_at"])
                    actual_wait = max(0.0, generated - started)
                    wait_state["model_generated_at"] = generated
                    admission = {
                        "request_id": event.get("request_id"),
                        "model_step": step,
                        "predicted_wait_seconds": prediction_wait,
                        "prediction_source": (
                            "configured_request_time_prediction"
                            if prediction_wait is not None else None
                        ),
                        "actual_wait_seconds": actual_wait,
                        "prediction_error_seconds": (
                            None if prediction_wait is None else actual_wait - prediction_wait
                        ),
                        "scheduled_restore_time": wait_state.get("scheduled_restore_time"),
                        "restore_started_at": wait_state.get("restore_started_at"),
                        "restore_completed_at": wait_state.get("restore_completed_at"),
                        "tool_call_count": len(message.get("tool_calls") or []),
                    }
                    return admission

            trace_path = (
                Path(arm.case.replay_trace_reference)
                if arm.inference.backend.value == "replay"
                and arm.case.replay_trace_reference else None
            )
            gateway_session = self.model_gateway.register(
                session_id=session_id,
                store_path=self.output_root / "model-gateway" / f"{session_id}.json",
                mode=arm.inference.backend.value,
                trace=trace_path,
                time_scale=float(arm.inference.configuration.get("time_scale", 1.0)),
                upstream_base_url=str(arm.inference.configuration.get("base_url") or "") or None,
                upstream_api_key=credential or None,
                upstream_model=str(arm.inference.configuration.get("model") or "") or None,
                on_request_started=on_model_request_started,
                before_response_ready=before_model_response_ready,
            )
            runtime_env["CLAWBOX_MODEL_GATEWAY_TOKEN"] = gateway_session.token
        # Delay coordinator registration until every pre-VM validation and
        # per-session gateway registration has succeeded.  Otherwise a bad
        # replay path or gateway setup error can leave a non-existent session
        # occupying policy state even though the main cleanup block was never
        # entered.
        coordinator.register(session_id, lifecycle)
        try:
            if lifetime:
                coordinator.acquire(session_id, lifetime, arm.execution.arm_timeout_seconds)
            timeline["sandbox_create_start"] = time.time()
            timeline["tool_create_start"] = time.time()
            tool_create_s = lifecycle.start()
            timeline["tool_ready"] = time.time()
            events.write({"event": "sandbox_created", "session_id": session_id,
                          "role": "tool", "service_seconds": tool_create_s,
                          "lifecycle_timing": lifecycle.timings[-1],
                          "sandbox_id": self.client.sandbox_id(lifecycle.sandbox)})
            tool_endpoint = None
            if arm.agent.driver is AgentDriver.OPENCLAW:
                tool_endpoint = self.client.get_tcp_endpoint(lifecycle.sandbox, 2222)
                _user, endpoint_host, _port = split_native_ssh_target(
                    native_ssh_target(tool_endpoint.address)
                )
                try:
                    endpoint_address = ipaddress.ip_address(endpoint_host)
                except ValueError as exc:
                    raise RuntimeError(
                        f"CubeSandbox returned a non-IP TCP endpoint host: {endpoint_host!r}"
                    ) from exc
                runtime_lifecycle.network_allow_out.append(
                    f"{endpoint_address}/{32 if endpoint_address.version == 4 else 128}"
                )
                runtime_lifecycle.network_allow_out = list(
                    dict.fromkeys(runtime_lifecycle.network_allow_out)
                )
                native_route_host = endpoint_host
            timeline["runtime_create_start"] = time.time()
            runtime_create_s = runtime_lifecycle.start()
            timeline["runtime_ready"] = time.time()
            timeline["sandbox_ready"] = timeline["runtime_ready"]
            events.write({"event": "sandbox_created", "session_id": session_id,
                          "role": "runtime", "service_seconds": runtime_create_s,
                          "lifecycle_timing": runtime_lifecycle.timings[-1],
                          "sandbox_id": self.client.sandbox_id(runtime_lifecycle.sandbox)})
            create_s = runtime_create_s + tool_create_s
            runtime_executor = CubeCommandExecutor(
                self.client, lambda: runtime_lifecycle.sandbox, cwd=arm.runtime.workspace,
            )
            executor = CubeCommandExecutor(self.client, lambda: lifecycle.sandbox, cwd=arm.sandbox.workspace)

            def refresh_native_ssh_route() -> None:
                nonlocal ssh_config
                if route_state is None or ssh_config is None or lifecycle.sandbox is None:
                    raise RuntimeError("native SSH route state is not initialized")
                discovery_started = time.monotonic()
                endpoint = self.client.get_tcp_endpoint(lifecycle.sandbox, 2222)
                target = native_ssh_target(endpoint.address)
                _user, host, port = split_native_ssh_target(target)
                if native_route_host is not None and host != native_route_host:
                    raise RuntimeError(
                        "CubeSandbox changed the semantic TCP endpoint host across restore; "
                        "the Runtime VM network allowlist cannot be mutated safely "
                        f"({native_route_host!r} -> {host!r})"
                    )
                discovery_seconds = time.monotonic() - discovery_started
                if target == route_state.get_target():
                    return
                known_host = f"[{host}]:{port} {ssh_credentials.host_public}\n"
                encoded = base64.b64encode(known_host.encode()).decode()
                known_host_started = time.monotonic()
                updated = runtime_executor.execute(
                    f"printf %s {shlex.quote(encoded)} | base64 -d > "
                    f"/state/openclaw/{session_id}/ssh/known_hosts.next && "
                    f"mv /state/openclaw/{session_id}/ssh/known_hosts.next "
                    f"/state/openclaw/{session_id}/ssh/known_hosts", 30,
                )
                if updated.exit_code:
                    raise RuntimeError(
                        "Runtime known-host refresh failed: " + updated.stderr[-1000:]
                    )
                known_host_seconds = time.monotonic() - known_host_started
                route_state.update(target)
                ssh_config = replace(ssh_config, target=target)
                events.write({
                    "event": "native_ssh_endpoint_refreshed", "session_id": session_id,
                    "container_port": endpoint.container_port,
                    "tcp_endpoint": endpoint.address,
                    "endpoint_discovery_seconds": discovery_seconds,
                    "known_host_update_seconds": known_host_seconds,
                    "source": "cubesandbox_tcp_endpoint",
                })

            exit_mismatches = 0
            model_steps = 0
            tool_latencies: list[float] = []
            admission_round_trip_seconds: list[float] = []
            admission_control_overhead_seconds: list[float] = []
            prediction_records: list[dict[str, Any]] = []
            observed_by_execution: dict[str, Any] = {}
            trace_writer = ClawTuneTraceWriter(
                self.output_root, run_id=self.run_id, session_id=session_id,
                repo_fingerprint=(arm.case.repository or str(
                    arm.inference.configuration.get("repo_fingerprint") or ""
                ) or None),
            )

            def execute_observed(command: str, timeout_s: float,
                                 execution_id: str, *,
                                 prediction: dict[str, Any] | None = None,
                                 phase: str = "agent"):
                observed = executor.execute_observed(
                    command, min(timeout_s, arm.execution.command_timeout_seconds),
                    execution_id=execution_id,
                )
                observed_by_execution[execution_id] = observed
                trace_writer.record(
                    command, observed.result, execution_id=execution_id,
                    bridge_record=observed.bridge_record, artifacts=observed.artifacts,
                    prediction=prediction, phase=phase,
                )
                events.write({
                    "event": "clawtune_observation", "session_id": session_id,
                    "execution_id": execution_id, "tool": "cube_shell",
                    "phase": phase,
                    "prediction": prediction,
                    "telemetry_state": observed.bridge_record.get("telemetry_state"),
                    "telemetry_unavailable_reason": observed.telemetry_unavailable_reason,
                    "artifact_kinds": sorted(observed.artifacts),
                })
                return observed.result

            timeline["agent_execution_start"] = time.time()
            model_trace_path: Path | None = None
            native_artifacts = None
            policy_control_path: Path | None = None
            if arm.agent.driver is AgentDriver.OPENCLAW:
                active_reservations: dict[str, int] = {}
                reservation_lock = Lock()

                def admit_openclaw_tool(request: dict[str, Any]) -> dict[str, Any]:
                    admission_started = time.monotonic()
                    execution_id = str(request["execution_id"])
                    prediction = (
                        prediction_provider.resolve_digest(
                            str(request["command_sha256"]), request.get("prediction")
                        ) if prediction_provider is not None else request.get("prediction")
                    )
                    amount = self._tool_reservation_mib(arm, prediction=prediction)
                    restore_seconds = 0.0
                    with wait_lock:
                        if not lifecycle.resident:
                            restore_started = time.monotonic()
                            self._restore_with_one_victim(
                                arm, session_id, lifecycle, coordinator, events,
                            )
                            refresh_native_ssh_route()
                            restore_seconds = time.monotonic() - restore_started
                        admission_wait = coordinator.acquire(
                            session_id, amount, arm.execution.arm_timeout_seconds
                        )
                        with reservation_lock:
                            active_reservations[execution_id] = amount
                            coordinator.set_tool_active(session_id, True)
                    prediction_record = dict(prediction or {})
                    prediction_record.update({
                                 "session_id": session_id,
                                 "execution_id": execution_id,
                                 "command_sha256": request["command_sha256"],
                                 "operation": request.get("operation"),
                                 "actual_measured_memory_mib": None,
                                 "admitted_reservation_mib": amount,
                                 "admission_blocked_seconds": admission_wait,
                    })
                    prediction_records.append(prediction_record)
                    events.write({
                        "event": "tool_admitted", "session_id": session_id,
                        "execution_id": execution_id,
                        "command_sha256": request["command_sha256"],
                        "prediction_key": prediction_record.get("canonical_prediction_key"),
                        "prediction_source": prediction_record.get("prediction_source"),
                        "fallback_level": prediction_record.get("fallback_level"),
                        "predicted_memory_mib": prediction_record.get(
                            "predicted_incremental_memory_mib"
                        ),
                        "admitted_memory_mib": amount,
                        "admission_blocked_seconds": admission_wait,
                        "restore_seconds": restore_seconds,
                        "policy_service_seconds": time.monotonic() - admission_started,
                    })
                    if route_state is None:
                        raise RuntimeError("native SSH route state is not initialized")
                    return {
                        "decision": "ADMIT",
                        "admitted_memory_mib": amount,
                        "admission_blocked_seconds": admission_wait,
                        "restore_seconds": restore_seconds,
                        "policy_service_seconds": time.monotonic() - admission_started,
                        # The Runtime shim applies this to the invocation that
                        # caused admission, closing the restore/old-port race.
                        "ssh_target": route_state.get_target(),
                    }

                def complete_openclaw_tool(request: dict[str, Any]) -> dict[str, Any]:
                    execution_id = str(request["execution_id"])
                    with wait_lock, reservation_lock:
                        amount = active_reservations.pop(execution_id)
                        coordinator.release(session_id, amount)
                        coordinator.set_tool_active(session_id, bool(active_reservations))

                    def eager_pause() -> None:
                        with wait_lock, reservation_lock:
                            if active_reservations or not lifecycle.resident:
                                return
                            pause_s = lifecycle.checkpoint_and_evict()
                            coordinator.pause_count += 1
                            coordinator.pause_service_seconds += pause_s
                            events.write({
                                "event": "sandbox_paused", "session_id": session_id,
                                "service_seconds": pause_s,
                                "reason": "openclaw_tool_complete",
                            })
                    if (arm.policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE
                            and arm.policy.eviction is EvictionPolicy.EAGER):
                        policy_events.submit(eager_pause)
                    events.write({"event": "tool_completed", "session_id": session_id,
                                  "execution_id": execution_id,
                                  "exit_code": request.get("exit_code")})
                    return {"status": "COMPLETED"}

                if self.policy_control is None:
                    raise RuntimeError("PolicyControlServer is not active")
                policy_session = self.policy_control.register(
                    session_id, admit=admit_openclaw_tool,
                    complete=complete_openclaw_tool,
                )
                tool_bridge_result = executor.execute(native_tool_bridge_setup_command(), 30)
                if tool_bridge_result.exit_code != 0:
                    raise RuntimeError(
                        "Tool setup could not start native SSH bridge: "
                        + tool_bridge_result.stderr[-1000:]
                    )
                if tool_endpoint is None:
                    raise RuntimeError("OpenClaw Tool endpoint was not resolved")
                ssh_target = native_ssh_target(tool_endpoint.address)
                route_state = NativeSSHRouteState(ssh_target)
                events.write({
                    "event": "native_ssh_endpoint_resolved", "session_id": session_id,
                    "target": ssh_target, "container_port": tool_endpoint.container_port,
                    "tcp_endpoint": tool_endpoint.address,
                    "phase": "setup", "source": "cubesandbox_tcp_endpoint",
                })
                ssh_config = NativeSSHConfig(
                    target=ssh_target,
                    identity_private_key=ssh_credentials.client_private,
                    host_public_key=ssh_credentials.host_public,
                    workspace_root=arm.sandbox.workspace,
                )
                outcome = run_openclaw(
                    prompt=arm.case.prompt, session_id=session_id,
                    configuration=arm.inference.configuration,
                    ssh=ssh_config,
                    policy_control=policy_session, runtime_executor=runtime_executor,
                    output_dir=self.output_root,
                    timeout_seconds=arm.execution.arm_timeout_seconds,
                    model_gateway=gateway_session,
                    prediction_manifest=(prediction_provider.manifest
                                         if prediction_provider is not None else None),
                )
                tool_latencies.extend(float(item) for item in outcome["tool_latencies"])
                admission_round_trip_seconds.extend(
                    float(item) for item in outcome["admission_round_trip_seconds"]
                )
                admission_control_overhead_seconds.extend(
                    float(item) for item in outcome["admission_control_overhead_seconds"]
                )
                policy_control_path = self.output_root / "policy-control" / f"{session_id}.json"
                atomic_json(policy_control_path, outcome["policy_control_records"])
                # A completed Agent call may have triggered eager Tool eviction.
                # Serialize restore and collection with delayed model-wait and
                # eager-pause callbacks so the Tool cannot disappear halfway
                # through the artifact stream.
                with wait_lock:
                    if not lifecycle.resident:
                        self._restore_with_one_victim(
                            arm, session_id, lifecycle, coordinator, events,
                        )
                        refresh_native_ssh_route()
                    native_artifacts = collect_and_validate_native_tool_artifacts(
                        runtime_executor=runtime_executor, ssh=ssh_config,
                        session_id=session_id, output_dir=self.output_root,
                        policy_records=outcome["policy_control_records"],
                        runtime_trace_paths=outcome["runtime_traces"],
                    )
                events.write({
                    "event": "native_tool_artifacts_collected",
                    "session_id": session_id,
                    "root": str(native_artifacts.root),
                    "validation": native_artifacts.validation,
                })
                if gateway_session is None:
                    raise RuntimeError("managed OpenClaw session has no ModelGateway state")
                model_steps = gateway_session.gateway.logical_model_steps()
                completeness = gateway_session.replay_completeness()
                if not completeness["complete"]:
                    raise RuntimeError(
                        "managed ModelGateway session incomplete: "
                        + json.dumps(completeness, sort_keys=True)
                    )
                if not tool_latencies:
                    raise RuntimeError("OpenClaw completed without using a Tool-VM workspace tool")
                if arm.inference.backend.value == "api":
                    model_trace_path = self.output_root / "model-traces" / f"{session_id}.jsonl"
                    gateway_session.write_replay_trace(model_trace_path)
                    events.write({
                        "event": "model_trace_recorded",
                        "session_id": session_id,
                        "path": str(model_trace_path),
                        "sha256": hashlib.sha256(model_trace_path.read_bytes()).hexdigest(),
                    })
                timeline["final_agent_completion"] = time.time()
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
                timeline["final_agent_completion"] = time.time()
            if not lifecycle.resident:
                self._restore_with_one_victim(arm, session_id, lifecycle, coordinator, events)
            validation = arm.validation.command or (
                arm.case.validation if isinstance(arm.case.validation, str) else None)
            valid = exit_mismatches == 0
            timeline.setdefault("agent_execution_start", timeline.get("sandbox_ready", time.time()))
            timeline["validation_start"] = time.time()
            if validation:
                validation_result = executor.execute(
                    validation, arm.execution.command_timeout_seconds,
                )
                valid = valid and validation_result.exit_code == 0
            timeline["validation_end"] = time.time()
            timeline["output_hash_start"] = time.time()
            hash_result = executor.execute(
                "find . -type f -exec sha256sum {} \\; | LC_ALL=C sort | sha256sum",
                arm.execution.command_timeout_seconds,
            )
            timeline["output_hash_end"] = time.time()
            timeline["output_hash_overhead_seconds"] = max(
                0.0, timeline["output_hash_end"] - timeline["validation_end"]
            )
            output_hash = (hash_result.stdout.split()[0] if hash_result.exit_code == 0
                           and hash_result.stdout.split() else hashlib.sha256(
                               json.dumps({"valid": valid, "mismatches": exit_mismatches},
                                          sort_keys=True).encode()).hexdigest())
            events.write({"event": "session_complete", "session_id": session_id, "valid": valid})
            if not valid:
                raise RuntimeError(f"validation failed for {session_id}")
            events.write({
                "event": "managed_session_measurement",
                "session_id": session_id,
                "model_steps": model_steps,
                "tool_steps": len(tool_latencies),
                "model_gateway_records": (
                    gateway_session.records() if gateway_session is not None else []
                ),
                "model_gateway_completeness": (
                    gateway_session.replay_completeness()
                    if gateway_session is not None else None
                ),
                "prediction_records": prediction_records,
                "prediction_provenance": (
                    prediction_provider.provenance(prediction_records)
                    if prediction_provider is not None else None
                ),
            })
            if policy_event_errors:
                raise RuntimeError("policy event executor failed: " + "; ".join(policy_event_errors))
            return {"session_id": session_id, "valid": valid, "create_seconds": create_s,
                    "runtime_create_seconds": runtime_create_s,
                    "tool_create_seconds": tool_create_s,
                    "duration_seconds": time.monotonic() - session_started,
                    "agent_jct_seconds": max(
                        0.0, timeline["final_agent_completion"] - timeline["agent_execution_start"]
                    ),
                    "provisioning_inclusive_jct_seconds": max(
                        0.0, timeline["final_agent_completion"] - timeline["sandbox_create_start"]
                    ),
                    "validation_overhead_seconds": max(
                        0.0, timeline["validation_end"] - timeline["final_agent_completion"]
                    ),
                    "tool_steps": len(tool_latencies), "model_steps": model_steps,
                    "tool_latencies": tool_latencies, "output_hash": output_hash,
                    "admission_round_trip_seconds": admission_round_trip_seconds,
                    "admission_control_overhead_seconds": admission_control_overhead_seconds,
                    "prediction_records": prediction_records,
                    "prediction_provenance": (
                        prediction_provider.provenance(prediction_records)
                        if prediction_provider is not None else None
                    ),
                    "model_gateway_records": (
                        gateway_session.records() if gateway_session is not None else []
                    ),
                    "model_gateway_completeness": (
                        gateway_session.replay_completeness()
                        if gateway_session is not None else None
                    ),
                     "model_trace_path": str(model_trace_path) if model_trace_path else None,
                     "policy_control_path": (
                         str(policy_control_path) if policy_control_path else None
                     ),
                     "runtime_trace_root": (
                         str(self.output_root / "runtime-traces" / session_id)
                         if arm.agent.driver is AgentDriver.OPENCLAW else None
                     ),
                     "native_tool_artifact_root": (
                         str(native_artifacts.root)
                         if arm.agent.driver is AgentDriver.OPENCLAW and native_artifacts
                         else None
                     ),
                     "native_tool_artifact_validation": (
                         native_artifacts.validation
                         if arm.agent.driver is AgentDriver.OPENCLAW and native_artifacts
                         else None
                     ),
                     "timeline": timeline}
        finally:
            timeline["sandbox_cleanup_start"] = time.time()
            with wait_lock:
                if wait_timer is not None:
                    wait_timer.cancel()
                if restore_timer is not None:
                    restore_timer.cancel()
            if gateway_session is not None and self.model_gateway is not None:
                self.model_gateway.unregister(gateway_session.token, timeout=30)
            if policy_session is not None:
                policy_drained = policy_session.close(timeout=30)
            try:
                tool_destroy_s = lifecycle.close()
                events.write({
                    "event": "sandbox_destroyed", "session_id": session_id,
                    "role": "tool", "service_seconds": tool_destroy_s,
                    "lifecycle_timing": lifecycle.timings[-1],
                })
            finally:
                try:
                    runtime_destroy_s = runtime_lifecycle.close()
                    events.write({
                        "event": "sandbox_destroyed", "session_id": session_id,
                        "role": "runtime", "service_seconds": runtime_destroy_s,
                        "lifecycle_timing": runtime_lifecycle.timings[-1],
                    })
                finally:
                    timeline["sandbox_cleanup_end"] = time.time()
                    timeline["session_finished"] = timeline["sandbox_cleanup_end"]
                    timeline["time_spans"] = build_time_spans(timeline)
                    events.write({
                        "event": "session_timing", "session_id": session_id,
                        "time_spans": timeline["time_spans"],
                        "runtime_lifecycle": runtime_lifecycle.timings,
                        "tool_lifecycle": lifecycle.timings,
                    })
                    if timeline.get("validation_end"):
                        timeline["cleanup_overhead_seconds"] = max(
                            0.0, timeline["sandbox_cleanup_end"] - timeline["validation_end"]
                        )
                    if lifetime:
                        coordinator.release(session_id, lifetime)
                    coordinator.unregister(session_id)
                    if not policy_drained:
                        raise RuntimeError(f"policy session did not drain: {session_id}")

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

    def _tool_reservation_mib(self, arm: ExperimentArm,
                              action: ReplayAction | None = None, *,
                              prediction: dict[str, Any] | None = None) -> int:
        policy = arm.policy.admission
        if policy is AdmissionPolicy.LIFETIME_FULL:
            return 0
        if policy is AdmissionPolicy.TOOL_FULL:
            return int(arm.resources.full_tool_memory_mib or arm.sandbox.memory_mib)
        if policy is AdmissionPolicy.TOOL_STATIC:
            return int(arm.resources.static_tool_memory_mib or 1)
        if policy is AdmissionPolicy.TOOL_P90 and prediction is None and arm.agent.driver is AgentDriver.OPENCLAW:
            raise PredictionUnavailable(
                "managed tool_p90 cannot admit without Runtime command prediction metadata"
            )
        source = arm.resources.p90_predictions if policy is AdmissionPolicy.TOOL_P90 else arm.resources.oracle_measurements
        if prediction is not None:
            value = prediction.get("predicted_incremental_memory_mib")
            if value is None:
                raise PredictionUnavailable("prediction metadata has no memory reservation")
            return max(1, int(math.ceil(float(value))))
        if action is None:
            raise ValueError("replay admission requires an action")
        payload = json.loads(Path(str(source)).read_text(encoding="utf-8"))
        if action.action_id not in payload:
            raise PredictionUnavailable(
                f"prediction artifact has no entry for replay action {action.action_id}"
            )
        return max(1, int(payload[action.action_id]))

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
    try:
        results = ExperimentWorker(load_experiment(args.spec), run_id=args.run_id,
                                   attempt_id=args.attempt_id, task_uid=args.task_uid).run()
    except Exception as exc:
        # Kubernetes otherwise records only exit code 1 when a pre-arm gate
        # fails (for example NodePort readiness). Keep the log useful without
        # printing credentials, prompts, request bodies, or bearer tokens.
        print(
            "experiment worker failed before completion: "
            f"run_id={args.run_id} attempt_id={args.attempt_id} "
            f"task_uid={args.task_uid} error_type={type(exc).__name__} "
            f"error={exc}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0 if all(result.status is RunStatus.SUCCEEDED for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
