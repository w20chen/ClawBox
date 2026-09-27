from __future__ import annotations

import json
import math
import shlex
from pathlib import Path

from clawbox.common.models import ExecutionIntent, Observation, ResourcePrediction


def _load_clawtune():
    from clawbox.clawtune_integration import use_clawtune
    use_clawtune()
    from tool_time.lattice_kb import LatticeTimeKB
    from tool_resource.runtime_kb import ToolCallQuery
    return LatticeTimeKB, ToolCallQuery


class TenantKnowledgeBase:
    """Tenant LatticeKB with explicitly reported static resource defaults."""

    def __init__(self, snapshot: str | None = None) -> None:
        LatticeKB, ToolCallQuery = _load_clawtune()
        self.ToolCallQuery = ToolCallQuery
        if snapshot:
            self.kb = LatticeKB.from_json_obj(json.loads(snapshot))
        else:
            from clawbox.clawtune_integration import seed_directory
            self.kb = LatticeKB.from_json_obj(json.loads(
                (seed_directory() / "clause-lattice-time-kb.json").read_text(encoding="utf-8")
            ))

    def predict(self, intent: ExecutionIntent, generation: int) -> ResourcePrediction:
        from clawbox.tuning.clawtune import predict_native_call_load
        values = predict_native_call_load(None, self.ToolCallQuery(
            repo=intent.repo_fingerprint, tool_name=intent.tool_name,
            command=intent.command, ts_start=intent.timestamp.timestamp(),
            memory_measurement="guest_memtotal_minus_memavailable",
        ), lattice=self.kb).targets
        cpu = values["cpu_peak_cores"]
        memory = values["memory_extra_peak_bytes"]
        duration = values["duration_ms"]
        def available(item):
            return item.status == "available" and item.p90 is not None and math.isfinite(item.p90) and item.p90 > 0
        cpu_p90 = float(cpu.p90) if available(cpu) else 4.0
        memory_bytes = float(memory.p90) if available(memory) else 512 * 1024**2
        duration_p90 = float(duration.p90) / 1000 if available(duration) else 1.0
        defaults = [name for name, item in (("cpu", cpu), ("memory", memory), ("duration", duration)) if not available(item)]
        counts = [item.sample_count for item in (cpu, memory, duration)]
        return ResourcePrediction(
            execution_id=intent.execution_id, cpu_p90=cpu_p90,
            memory_p90=int(memory_bytes), duration_p50=float(duration.p50) / 1000 if available(duration) else 0.7,
            duration_p90=duration_p90, time_bucket=self._bucket(duration_p90),
            match_level="lattice_with_static_defaults" if defaults else "lattice",
            prediction_backend="lattice", defaulted_targets=defaults,
            sample_count=min(counts) if counts else 0,
            confidence=min(1.0, (min(counts) if counts else 0) / 10),
            kb_generation=generation,
        )

    def observe(self, intent: ExecutionIntent, observation: Observation) -> bool:
        from clawbox.tuning.native import _clawtune_api, native_clause_observations
        artifact = observation.clause_telemetry
        if not artifact:
            return False
        _clawtune_api()[5](Path("observation.clause_telemetry"), artifact,
                          expected_repo=intent.repo_fingerprint)
        calls = artifact.get("calls", [])
        if (len(calls) != 1 or calls[0].get("tool_call_id") != intent.execution_id
                or calls[0].get("command") != intent.command
                or calls[0].get("eligible_for_kb") is not True):
            raise ValueError("clause telemetry does not match execution")
        rows = native_clause_observations(intent.repo_fingerprint, calls[0], observation.memory)
        for row in rows:
            self.kb.observe_completed_clause(row)
        return bool(rows)

    def snapshot(self) -> str:
        return json.dumps(self.kb.to_json_obj(), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def argv(command: str) -> list[str]:
        try: return shlex.split(command)
        except ValueError: return []

    @staticmethod
    def _bucket(seconds: float) -> str:
        return "short" if seconds <= 1 else "medium" if seconds <= 30 else "long"
