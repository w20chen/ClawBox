from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .latency import LinearLatencyPredictor
from .lifecycle import CommandExecutor, SandboxLifecycle
from .trace import ReplayAction


@dataclass(frozen=True, slots=True)
class SnapshotPolicy:
    min_predicted_llm_s: float = 20.0
    estimated_snapshot_s: float = 1.0
    estimated_restore_s: float = 1.0
    safety_margin_s: float = 2.0

    def should_snapshot(self, predicted_s: float) -> bool:
        break_even = self.estimated_snapshot_s + self.estimated_restore_s + self.safety_margin_s
        return predicted_s >= max(self.min_predicted_llm_s, break_even)


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    mode: str
    action_count: int
    llm_count: int
    tool_count: int
    snapshots: int
    recorded_llm_s: float
    slept_llm_s: float
    snapshot_s: float
    restore_s: float
    tool_s: float
    tool_wait_s: float
    wall_s: float
    exit_mismatches: int


class ReplayEngine:
    def __init__(self, *, lifecycle: SandboxLifecycle, executor: CommandExecutor,
                 predictor: LinearLatencyPredictor, policy: SnapshotPolicy,
                 mode: str, sleep_scale: float = 1.0,
                 tool_time_scale: float = 0.0,
                 command_timeout_s: float = 300.0,
                 strict_exit_codes: bool = True,
                 event_sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        if mode not in {"resident", "snapshot"}:
            raise ValueError("mode must be resident or snapshot")
        if sleep_scale < 0:
            raise ValueError("sleep_scale must be non-negative")
        if tool_time_scale < 0:
            raise ValueError("tool_time_scale must be non-negative")
        self.lifecycle = lifecycle
        self.executor = executor
        self.predictor = predictor
        self.policy = policy
        self.mode = mode
        self.sleep_scale = sleep_scale
        self.tool_time_scale = tool_time_scale
        self.command_timeout_s = command_timeout_s
        self.strict_exit_codes = strict_exit_codes
        self.event_sink = event_sink or (lambda event: None)

    def run(self, actions: Iterable[ReplayAction]) -> ReplaySummary:
        action_list = list(actions)
        started = time.monotonic()
        snapshot_s = restore_s = tool_s = tool_wait_s = slept_llm_s = 0.0
        snapshots = exit_mismatches = 0
        try:
            self.event_sink({"event": "sandbox_start", "elapsed_s": self.lifecycle.start()})
            self._wait_executor_ready("sandbox_ready")
            for index, action in enumerate(action_list):
                if action.kind == "llm":
                    llm_started = time.monotonic()
                    predicted = self.predictor.predict(action)
                    decision = self.mode == "snapshot" and self.policy.should_snapshot(predicted)
                    self.event_sink({
                        "event": "llm_begin", "index": index, "action_id": action.action_id,
                        "recorded_s": action.duration_s, "predicted_s": predicted,
                        "snapshot": decision, "input_chars": action.input_chars,
                    })
                    if decision:
                        elapsed = self.lifecycle.checkpoint_and_evict()
                        snapshot_s += elapsed
                        snapshots += 1
                        self.event_sink({"event": "sandbox_evicted", "index": index, "elapsed_s": elapsed})
                    # Inference starts before checkpointing. Snapshot time is
                    # therefore inside, not in addition to, the recorded LLM
                    # latency window. Restore starts once the result is ready.
                    llm_deadline = llm_started + action.duration_s * self.sleep_scale
                    sleep_s = max(0.0, llm_deadline - time.monotonic())
                    time.sleep(sleep_s)
                    slept_llm_s += sleep_s
                    if decision:
                        elapsed = self.lifecycle.restore()
                        restore_s += elapsed
                        self.event_sink({"event": "sandbox_restored", "index": index, "elapsed_s": elapsed})
                        self._wait_executor_ready("sandbox_ready", index=index)
                    self.event_sink({"event": "llm_end", "index": index})
                elif action.kind == "tool":
                    if not self.lifecycle.resident:
                        raise RuntimeError(f"sandbox is not resident before tool action {action.action_id}")
                    command = action.shell_command()
                    result = self.executor.execute(command, self.command_timeout_s)
                    tool_s += result.duration_s
                    mismatch = (
                        action.expected_exit_code is not None
                        and action.expected_exit_code != result.exit_code
                    )
                    exit_mismatches += int(mismatch)
                    self.event_sink({
                        "event": "tool_end", "index": index, "action_id": action.action_id,
                        "name": action.name, "exit_code": result.exit_code,
                        "expected_exit_code": action.expected_exit_code,
                        "exit_mismatch": mismatch, "elapsed_s": result.duration_s,
                        "stdout_bytes": len(result.stdout.encode()),
                        "stderr_bytes": len(result.stderr.encode()),
                    })
                    if mismatch and self.strict_exit_codes:
                        raise RuntimeError(
                            f"tool action {action.action_id} exit mismatch: "
                            f"expected {action.expected_exit_code}, got {result.exit_code}"
                        )
                    wait_s = max(0.0, action.duration_s * self.tool_time_scale - result.duration_s)
                    time.sleep(wait_s)
                    tool_wait_s += wait_s
                else:
                    raise ValueError(f"unsupported replay action kind {action.kind!r}")
        finally:
            self.lifecycle.close()
        summary = ReplaySummary(
            mode=self.mode,
            action_count=len(action_list),
            llm_count=sum(action.kind == "llm" for action in action_list),
            tool_count=sum(action.kind == "tool" for action in action_list),
            snapshots=snapshots,
            recorded_llm_s=sum(action.duration_s for action in action_list if action.kind == "llm"),
            slept_llm_s=slept_llm_s,
            snapshot_s=snapshot_s,
            restore_s=restore_s,
            tool_s=tool_s,
            tool_wait_s=tool_wait_s,
            wall_s=time.monotonic() - started,
            exit_mismatches=exit_mismatches,
        )
        self.event_sink({"event": "summary", **asdict(summary)})
        return summary

    def _wait_executor_ready(self, event: str, **values: Any) -> None:
        wait_ready = getattr(self.executor, "wait_ready", None)
        if callable(wait_ready):
            elapsed = float(wait_ready(self.command_timeout_s))
            self.event_sink({"event": event, "elapsed_s": elapsed, **values})


class JsonlEventWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w", encoding="utf-8")

    def __call__(self, event: dict[str, Any]) -> None:
        record = {"wall_time_s": time.time(), **event}
        self._handle.write(json.dumps(record, sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()
