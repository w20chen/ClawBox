from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .guest import RuntimeAgent
from .inference import InferenceProvider, TraceReplayInferenceProvider
from .latency import LinearLatencyPredictor
from .lifecycle import CommandExecutor, SandboxLifecycle
from .trace import ReplayAction


@dataclass(frozen=True, slots=True)
class SnapshotPolicy:
    min_predicted_llm_s: float = 20.0
    estimated_snapshot_s: float = 1.0
    estimated_restore_s: float = 1.0
    estimated_refault_s: float = 0.0
    safety_margin_s: float = 2.0

    def should_snapshot(self, predicted_s: float) -> bool:
        break_even = (
            self.estimated_snapshot_s + self.estimated_restore_s
            + self.estimated_refault_s + self.safety_margin_s
        )
        return predicted_s >= max(self.min_predicted_llm_s, break_even)


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    mode: str
    action_count: int
    llm_count: int
    tool_count: int
    snapshots: int
    tool_snapshots: int
    recorded_llm_s: float
    slept_llm_s: float
    snapshot_s: float
    restore_s: float
    tool_snapshot_s: float
    tool_restore_s: float
    tool_s: float
    tool_wait_s: float
    wall_s: float
    exit_mismatches: int


class ReplayEngine:
    def __init__(self, *, lifecycle: SandboxLifecycle, executor: CommandExecutor,
                 predictor: LinearLatencyPredictor, policy: SnapshotPolicy,
                 mode: str, sleep_scale: float = 1.0,
                 tool_time_scale: float = 0.0,
                 inference_provider: InferenceProvider | None = None,
                 runtime_agent: RuntimeAgent | None = None,
                 tool_lifecycle: SandboxLifecycle | None = None,
                 tool_runtime_agent: RuntimeAgent | None = None,
                 tool_policy: SnapshotPolicy | None = None,
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
        self.inference_provider = inference_provider or TraceReplayInferenceProvider(
            time_scale=sleep_scale,
        )
        self.runtime_agent = runtime_agent
        self.tool_lifecycle = tool_lifecycle
        self.tool_runtime_agent = tool_runtime_agent
        self.tool_policy = tool_policy or policy
        if tool_runtime_agent is not None and tool_lifecycle is None:
            raise ValueError("tool_runtime_agent requires tool_lifecycle")
        self.command_timeout_s = command_timeout_s
        self.strict_exit_codes = strict_exit_codes
        self.event_sink = event_sink or (lambda event: None)

    def run(self, actions: Iterable[ReplayAction]) -> ReplaySummary:
        action_list = list(actions)
        started = time.monotonic()
        snapshot_s = restore_s = tool_snapshot_s = tool_restore_s = 0.0
        tool_s = tool_wait_s = slept_llm_s = 0.0
        snapshots = tool_snapshots = exit_mismatches = 0
        try:
            self.event_sink({"event": "sandbox_start", "elapsed_s": self.lifecycle.start()})
            self._wait_runtime_ready("runtime_agent_ready")
            if self.tool_lifecycle is not None:
                self.event_sink({
                    "event": "tool_sandbox_start",
                    "elapsed_s": self.tool_lifecycle.start(),
                })
                self._wait_tool_runtime_ready("tool_runtime_agent_ready")
            # For paired direct Firecracker, this reaches the Tool guest only
            # after its lifecycle is resident.  Other executors keep their
            # original start-of-session health check here.
            self._wait_executor_ready("sandbox_ready")
            for index, action in enumerate(action_list):
                if action.kind == "llm":
                    predicted = self.predictor.predict(action)
                    inference = self.inference_provider.begin(action, predicted)
                    decision_wait_s = inference.estimated_wait_s
                    decision = (
                        self.mode == "snapshot"
                        and self.policy.should_snapshot(decision_wait_s)
                    )
                    tool_decision = (
                        self.mode == "snapshot"
                        and self.tool_lifecycle is not None
                        and self.tool_policy.should_snapshot(decision_wait_s)
                    )
                    before_state = None
                    if self.runtime_agent is not None:
                        before_state = self.runtime_agent.begin_llm(
                            inference.request_id, predicted, inference.metadata,
                        )
                    tool_before_state = None
                    if self.tool_runtime_agent is not None:
                        tool_before_state = self.tool_runtime_agent.begin_llm(
                            inference.request_id, predicted, inference.metadata,
                        )
                    self.event_sink({
                        "event": "llm_begin", "index": index, "action_id": action.action_id,
                        "recorded_s": action.duration_s, "predicted_s": predicted,
                        "decision_wait_s": decision_wait_s,
                        "snapshot": decision, "input_chars": action.input_chars,
                        "tool_snapshot": tool_decision,
                        "inference_metadata": inference.metadata,
                        "guest_boot_nonce": None if before_state is None else before_state.boot_nonce,
                    })
                    if decision:
                        elapsed = self.lifecycle.checkpoint_and_evict()
                        snapshot_s += elapsed
                        snapshots += 1
                        self.event_sink({"event": "sandbox_evicted", "index": index, "elapsed_s": elapsed})
                    if tool_decision:
                        if self.tool_lifecycle is None:
                            raise RuntimeError("tool snapshot selected without a tool lifecycle")
                        elapsed = self.tool_lifecycle.checkpoint_and_evict()
                        tool_snapshot_s += elapsed
                        tool_snapshots += 1
                        self.event_sink({
                            "event": "tool_sandbox_evicted", "index": index,
                            "elapsed_s": elapsed,
                        })
                    wait_started = time.monotonic()
                    inference_result = inference.wait_ready()
                    slept_llm_s += time.monotonic() - wait_started
                    if tool_decision:
                        if self.tool_lifecycle is None:
                            raise RuntimeError("tool restore selected without a tool lifecycle")
                        elapsed = self.tool_lifecycle.restore()
                        tool_restore_s += elapsed
                        self.event_sink({
                            "event": "tool_sandbox_restored", "index": index,
                            "elapsed_s": elapsed,
                        })
                        self._wait_tool_runtime_ready("tool_runtime_agent_ready", index=index)
                        # A reconnecting Tool executor can only probe after the
                        # Tool VM is restored; the old vsock connection is gone.
                        self._wait_executor_ready("tool_executor_ready", index=index)
                    if decision:
                        elapsed = self.lifecycle.restore()
                        restore_s += elapsed
                        self.event_sink({"event": "sandbox_restored", "index": index, "elapsed_s": elapsed})
                        self._wait_runtime_ready("runtime_agent_ready", index=index)
                    guest_state = None
                    if self.runtime_agent is not None:
                        if before_state is None:
                            raise RuntimeError("runtime agent state was not captured before inference")
                        self.runtime_agent.assert_inflight(
                            inference_result.request_id, before_state.boot_nonce,
                        )
                        guest_state = self.runtime_agent.complete_llm(inference_result.request_id)
                    tool_guest_state = None
                    if self.tool_runtime_agent is not None:
                        if tool_before_state is None:
                            raise RuntimeError("tool runtime state was not captured before inference")
                        self.tool_runtime_agent.assert_inflight(
                            inference_result.request_id, tool_before_state.boot_nonce,
                        )
                        tool_guest_state = self.tool_runtime_agent.complete_llm(
                            inference_result.request_id,
                        )
                    self.event_sink({
                        "event": "llm_end", "index": index,
                        "request_id": inference_result.request_id,
                        "guest_turn": None if guest_state is None else guest_state.turn,
                        "tool_guest_turn": (
                            None if tool_guest_state is None else tool_guest_state.turn
                        ),
                    })
                elif action.kind == "tool":
                    if not self.lifecycle.resident:
                        raise RuntimeError(f"sandbox is not resident before tool action {action.action_id}")
                    if self.tool_lifecycle is not None and not self.tool_lifecycle.resident:
                        raise RuntimeError(
                            f"tool sandbox is not resident before tool action {action.action_id}"
                        )
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
                    if self.runtime_agent is not None:
                        self.runtime_agent.tool_completed(action.action_id, result.exit_code)
                    if self.tool_runtime_agent is not None:
                        self.tool_runtime_agent.tool_completed(action.action_id, result.exit_code)
                    wait_s = max(0.0, action.duration_s * self.tool_time_scale - result.duration_s)
                    time.sleep(wait_s)
                    tool_wait_s += wait_s
                else:
                    raise ValueError(f"unsupported replay action kind {action.kind!r}")
        finally:
            try:
                if self.tool_lifecycle is not None:
                    self.tool_lifecycle.close()
            finally:
                self.lifecycle.close()
        summary = ReplaySummary(
            mode=self.mode,
            action_count=len(action_list),
            llm_count=sum(action.kind == "llm" for action in action_list),
            tool_count=sum(action.kind == "tool" for action in action_list),
            snapshots=snapshots,
            tool_snapshots=tool_snapshots,
            recorded_llm_s=sum(action.duration_s for action in action_list if action.kind == "llm"),
            slept_llm_s=slept_llm_s,
            snapshot_s=snapshot_s,
            restore_s=restore_s,
            tool_snapshot_s=tool_snapshot_s,
            tool_restore_s=tool_restore_s,
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

    def _wait_runtime_ready(self, event: str, **values: Any) -> None:
        if self.runtime_agent is None:
            return
        elapsed = float(self.runtime_agent.wait_ready(self.command_timeout_s))
        self.event_sink({"event": event, "elapsed_s": elapsed, **values})

    def _wait_tool_runtime_ready(self, event: str, **values: Any) -> None:
        if self.tool_runtime_agent is None:
            return
        elapsed = float(self.tool_runtime_agent.wait_ready(self.command_timeout_s))
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
