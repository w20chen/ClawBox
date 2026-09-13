from __future__ import annotations

import json
import shlex
from datetime import datetime

from clawbox.common.models import ExecutionIntent, Observation, ResourcePrediction


def _load_clawtune():
    from clawbox.clawtune_integration import use_clawtune
    use_clawtune()
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
                CompletedCall("public", "exec", "true", 0, 1, cpu_peak_cores=4,
                              cpu_peak_cores_eligible=True, cpu_peak_window_ms=500),
                CompletedCall("public", "exec", "python -m pytest", 0, 30,
                              cpu_peak_cores=4, cpu_peak_cores_eligible=True,
                              cpu_peak_window_ms=500),
            ]
            self.kb = RuntimeKB.fit_public(baseline)

    def predict(self, intent: ExecutionIntent, generation: int) -> ResourcePrediction:
        values = self.kb.query(self.ToolCallQuery(
            repo=intent.repo_fingerprint, tool_name=intent.tool_name,
            command=intent.command, ts_start=intent.timestamp.timestamp(),
        ))
        cpu = values["cpu_peak_cores"]
        memory = values["memory_extra_peak_bytes"]
        duration = values["latency_ms"]
        cpu_p90 = max(0.1, float(cpu.conditional_p90 or 4))
        memory_bytes = max(1, float(memory.conditional_p90 or 512 * 1024**2))
        duration_p90 = max(0, float(duration.conditional_p90 or 1000) / 1000)
        scopes = [item.scope for item in (cpu, memory, duration) if item.scope]
        counts = [item.evidence_count for item in (cpu, memory, duration)]
        return ResourcePrediction(
            execution_id=intent.execution_id, cpu_p90=cpu_p90,
            memory_p90=int(memory_bytes), duration_p50=duration_p90 * 0.7,
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
            cpu_peak_cores=float(cpu) if cpu is not None else None,
            cpu_peak_cores_eligible=cpu is not None,
            cpu_peak_window_ms=500 if cpu is not None else None,
            memory_eligible=False,
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
