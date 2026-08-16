from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path

from clawbox.common.models import ExecutionIntent, Observation, ResourcePrediction


def _load_clawtune():
    candidates = [
        os.getenv("CLAWTUNE_SIDECAR_SRC"),
        # Backward-compatible only for an operator upgrading an older install.
        os.getenv("CLAWTUNE_SCHEDULER_SRC"),
        str(Path(__file__).resolve().parents[3] / "ClawTune" / "services" / "sidecar" / "src"),
        "/opt/clawtune/services/sidecar/src",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir() and candidate not in sys.path:
            sys.path.insert(0, candidate)
    from tool_resource.runtime_kb import CompletedCall, RuntimeToolResourceKB, ToolCallQuery
    return CompletedCall, RuntimeToolResourceKB, ToolCallQuery


class TenantKnowledgeBase:
    """Thin tenant overlay around ClawTune's unchanged RuntimeToolResourceKB."""

    def __init__(self, snapshot: str | None = None) -> None:
        CompletedCall, RuntimeKB, ToolCallQuery = _load_clawtune()
        self.CompletedCall = CompletedCall
        self.ToolCallQuery = ToolCallQuery
        if snapshot:
            self.kb = RuntimeKB.from_json_obj(json.loads(snapshot))
        else:
            # Compatible public baseline. Tenant observations only enter the
            # private repo layer and are never numerically blended into it.
            baseline = [
                CompletedCall("public", "exec", "true", 0, 1, peak_cpu_cores=4,
                              peak_cpu_cores_eligible=True, peak_memory_mb=512,
                              peak_memory_mb_eligible=True, ambient_before_mb=0),
                CompletedCall("public", "exec", "python -m pytest", 0, 30,
                              peak_cpu_cores=4, peak_cpu_cores_eligible=True,
                              peak_memory_mb=512, peak_memory_mb_eligible=True,
                              ambient_before_mb=0),
            ]
            self.kb = RuntimeKB.fit_public(baseline)

    def predict(self, intent: ExecutionIntent, generation: int) -> ResourcePrediction:
        values = self.kb.query(self.ToolCallQuery(
            repo=intent.repo_fingerprint, tool_name=intent.tool_name,
            command=intent.command, ts_start=intent.timestamp.timestamp(), ambient_before_mb=0,
        ))
        cpu = values["peak_cpu_cores"]
        memory = values["peak_memory_mb"]
        duration = values["latency_ms"]
        cpu_p90 = max(0.1, float(cpu.conditional_p90 or 4))
        memory_mb = max(1, float(memory.conditional_p90 or 512))
        duration_p90 = max(0, float(duration.conditional_p90 or 1000) / 1000)
        scopes = [item.scope for item in (cpu, memory, duration) if item.scope]
        counts = [item.evidence_count for item in (cpu, memory, duration)]
        return ResourcePrediction(
            execution_id=intent.execution_id, cpu_p90=cpu_p90,
            memory_p90=int(memory_mb * 1024**2), duration_p50=duration_p90 * 0.7,
            duration_p90=duration_p90, time_bucket=self._bucket(duration_p90),
            match_level=scopes[0] if scopes else "global_default",
            sample_count=min(counts) if counts else 0,
            confidence=min(1.0, (min(counts) if counts else 0) / 10),
            kb_generation=generation,
        )

    def observe(self, intent: ExecutionIntent, observation: Observation) -> None:
        cpu = observation.cpu.get("peak_cores")
        memory = observation.memory.get("peak_bytes")
        self.kb.observe_completed_call(self.CompletedCall(
            repo=intent.repo_fingerprint, tool_name=intent.tool_name, command=intent.command,
            ts_start=observation.start_time.timestamp(), ts_end=observation.end_time.timestamp(),
            censored=not observation.complete or observation.exit_code != 0,
            peak_cpu_cores=float(cpu) if cpu is not None else None,
            peak_cpu_cores_eligible=cpu is not None,
            peak_memory_mb=float(memory) / 1024**2 if memory is not None else None,
            peak_memory_mb_eligible=memory is not None, ambient_before_mb=0,
        ))

    def snapshot(self) -> str:
        return json.dumps(self.kb.to_json_obj(), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def argv(command: str) -> list[str]:
        try: return shlex.split(command)
        except ValueError: return []

    @staticmethod
    def _bucket(seconds: float) -> str:
        return "short" if seconds <= 1 else "medium" if seconds <= 30 else "long"

