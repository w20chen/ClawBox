"""ClawTune-compatible observations for commands executed by CubeSandbox.

The Worker is the trust boundary: OpenClaw requests a tool call, CubeSandbox
executes it, and only the Worker records the authoritative result.  ClawTune
consumes these records offline; it never receives command execution or sandbox
lifecycle authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from clawbox.replay.lifecycle import CommandResult


_EXECUTION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class ClawTuneTraceWriter:
    """Write exact-ID v6 spans and matching Cube tool-bridge records."""

    def __init__(self, root: Path, *, run_id: str, session_id: str,
                 repo_fingerprint: str | None = None) -> None:
        self.trace_path = root / "traces" / f"{session_id}.jsonl"
        self.bridge_path = root / "tool-bridge.jsonl"
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.session_id = session_id
        self.repo_fingerprint = repo_fingerprint
        self._native = None
        self._lock = Lock()

    def record(self, command: str, result: CommandResult, *,
               execution_id: str | None = None,
               bridge_record: dict | None = None,
               artifacts: dict[str, str] | None = None,
               prediction: dict | None = None,
               tool_name: str = "exec",
               phase: str = "agent") -> str:
        """Record one completed Cube command and return its execution ID."""
        execution_id = execution_id or f"cube-{uuid.uuid4().hex}"
        if not _EXECUTION_ID.fullmatch(execution_id):
            raise ValueError("execution_id must be 1-128 safe ASCII identifier characters")
        tool_name = str(tool_name or "exec").strip()
        if not tool_name or len(tool_name) > 128:
            raise ValueError("tool_name must be between 1 and 128 characters")
        digest = hashlib.sha256(command.encode()).hexdigest()
        cgroup = self._artifact(artifacts, "cgroup_resource_v1", execution_id)
        pmu = self._artifact(artifacts, "pmu_profile_v1", execution_id)
        if pmu is None and isinstance(cgroup, dict) and isinstance(cgroup.get("pmu"), dict):
            pmu = cgroup["pmu"]
        with self._lock:
            self._record_native(command, result, execution_id, cgroup, pmu, prediction, tool_name)
            bridge = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cell_id": None,
                "task_id": self.session_id,
                "execution_id": execution_id,
                "execution_source": "runtime-envelope",
                "command_sha256": digest,
                "command_bytes": len(command.encode()),
                "duration_ms": max(0, round(result.duration_s * 1000)),
                "exit_code": result.exit_code,
                "timed_out": result.exit_code == 124,
                "stdout_bytes": len(result.stdout.encode()),
                "stderr_bytes": len(result.stderr.encode()),
                "output_truncated": False,
                "phase": phase,
            }
            if bridge_record:
                bridge.update(bridge_record)
            if prediction is not None:
                bridge["prediction"] = prediction
            bridge["execution_id"] = execution_id
            self._append(self.bridge_path, bridge)
            self._persist_artifacts(execution_id, artifacts or {})
        return execution_id

    def close(self) -> None:
        if self._native is not None:
            self._native.close()
            self._native = None

    def _record_native(self, command, result, execution_id, cgroup, pmu, prediction, tool_name):
        from clawbox.clawtune_integration import use_clawtune
        use_clawtune()
        from clawtune_sidecar.trace import AgentTestBenchTraceWriter
        from clawtune_sidecar.contracts.models import ToolBeforeRequest, ToolCompletedEvent, ToolPrediction, ResourceScope
        from clawtune_sidecar.monitoring.tool_runtime import ToolRuntimeSample

        if self._native is None:
            self._native = AgentTestBenchTraceWriter(
                self.trace_path.parent, scaffold="clawbox", default_repo=self.repo_fingerprint or "unknown",
            )
        end = time.time()
        start = end - max(0, result.duration_s)
        duration_ms = max(0, round(result.duration_s * 1000))
        common = dict(schema_version="clawtune.v1", event_id=execution_id,
                      occurred_at=datetime.now(timezone.utc).isoformat(), plugin_version="clawbox",
                      run_id=self.run_id, session_id=self.session_id, session_key=None,
                      agent_id=self.session_id, runtime_id=self.session_id, gateway_id=self.session_id,
                      repo=self.repo_fingerprint, tool_call_id=execution_id, tool_name=tool_name)
        scope = ResourceScope(kind="cgroup-v2", execution_id=execution_id,
                              cgroup_path=cgroup["cgroup_path"], source="clawbox_tool_vm") if cgroup and cgroup.get("cgroup_path") else None
        before = ToolBeforeRequest(**common, tool_kind="shell", tool_input_kind="command",
            derived_paths=[], params_digest=hashlib.sha256(command.encode()).hexdigest(),
            param_features=dict(serialized_size_bytes=len(command.encode()), string_length=len(command),
                                list_item_count=0, path_count=0, has_command_like_field=True),
            raw_params={"command": command}, resource_scope=scope)
        after = ToolCompletedEvent(**common, decision_id=None, lease_id=None, execution_id=execution_id,
            duration_ms=duration_ms, succeeded=result.exit_code == 0, resource_scope=scope,
            error_type="timeout" if result.exit_code == 124 else None, error_digest=None,
            raw_result={"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr})
        c = cgroup or {}
        # Only guest measurements populate resource fields; RPC time is latency.
        sample = ToolRuntimeSample(
            event_id=execution_id, tool_call_id=execution_id, tool_name=tool_name, operation=None,
            started_at=start, ended_at=end, duration_ms=duration_ms, monitor_duration_ms=duration_ms,
            monitor_start_wall_s=start, monitor_end_wall_s=end,
            monitor_start_monotonic_s=None, monitor_end_monotonic_s=None,
            cpu_time_delta_s=c.get("cpu_time_s"), rss_bytes_before=c.get("memory_rss_before_bytes"),
            rss_bytes_after=c.get("memory_rss_after_bytes"), rss_bytes_peak=c.get("memory_rss_peak_bytes"),
            cpu_utilization_avg_cores=c.get("cpu_utilization_avg_cores"), cpu_utilization_avg_pct=None,
            read_bytes_delta=None, write_bytes_delta=None, net_rx_bytes_delta=None, net_tx_bytes_delta=None,
            ctx_switches_delta=None, disk_read_bytes_per_s=None, disk_write_bytes_per_s=None,
            net_rx_bytes_per_s=None, net_tx_bytes_per_s=None, sampling_interval_ms=0, sampling_point_count=0,
            sampling_quality=c.get("sampling_quality", "unknown"), resource_timeline=[],
            resource_timeline_truncated=False, resource_class="unknown", target_pid=None,
            process_count_before=None, process_count_after=None,
            attribution_status="cgroup-v2" if cgroup else "unattributed",
            monitor_source="cgroup-v2" if cgroup else "cube_rpc", pmu_profile=pmu,
        )
        self._native.record_tool_started(before)
        if prediction is not None and set(prediction).issubset(ToolPrediction.model_fields):
            self._native.record_tool_prediction(before, ToolPrediction.model_validate(prediction))
        self._native.record_tool(after, sample)
        if not self._native.flush():
            raise RuntimeError("ClawTune trace recorder did not flush")

    @staticmethod
    def _artifact(artifacts: dict[str, str] | None, kind: str,
                  execution_id: str) -> dict | None:
        try:
            value = json.loads((artifacts or {})[kind])
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        if kind == "clause_telemetry_v2":
            calls = value.get("calls")
            if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
                return None
            if calls[0].get("tool_call_id") != execution_id:
                return None
        elif value.get("execution_id") != execution_id:
            return None
        return value

    def _persist_artifacts(self, execution_id: str, artifacts: dict[str, str]) -> None:
        target_dir = self.trace_path.parent / "tool-resource"
        target_dir.mkdir(parents=True, exist_ok=True)
        names = {
            "cgroup_resource_v1": "cgroup-resource",
            "clause_telemetry_v2": "clause-telemetry",
            "pmu_profile_v1": "pmu-profile",
        }
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", execution_id)
        for kind, prefix in names.items():
            value = self._artifact(artifacts, kind, execution_id)
            if value is None:
                continue
            target = target_dir / f"{prefix}-{safe_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(artifacts[kind], encoding="utf-8", newline="")
            temporary.replace(target)

    @staticmethod
    def _append(path: Path, value: dict) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
