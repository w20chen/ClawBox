from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .trace import ReplayAction


@dataclass(frozen=True, slots=True)
class InferenceResult:
    request_id: str
    predicted_s: float
    simulated_s: float
    metadata: dict[str, Any] = field(default_factory=dict)


class InferenceWait(Protocol):
    @property
    def request_id(self) -> str: ...
    @property
    def predicted_s(self) -> float: ...
    @property
    def estimated_wait_s(self) -> float: ...
    @property
    def metadata(self) -> dict[str, Any]: ...
    def wait_ready(self) -> InferenceResult: ...


class InferenceProvider(Protocol):
    def begin(self, action: ReplayAction, predicted_s: float) -> InferenceWait: ...


class _TraceWait:
    def __init__(self, *, request_id: str, predicted_s: float,
                 simulated_s: float, time_scale: float,
                 metadata: dict[str, Any]) -> None:
        self._request_id = request_id
        self._predicted_s = predicted_s
        self._simulated_s = simulated_s
        self._time_scale = time_scale
        self._metadata = metadata
        self._deadline = time.monotonic() + simulated_s

    @property
    def request_id(self) -> str:
        return self._request_id

    @property
    def predicted_s(self) -> float:
        return self._predicted_s

    @property
    def estimated_wait_s(self) -> float:
        return self._predicted_s * self._time_scale

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    def wait_ready(self) -> InferenceResult:
        time.sleep(max(0.0, self._deadline - time.monotonic()))
        return InferenceResult(
            request_id=self._request_id,
            predicted_s=self._predicted_s,
            simulated_s=self._simulated_s,
            metadata=dict(self._metadata),
        )


class TraceReplayInferenceProvider:
    """Replay recorded inference timing behind a future GPU-compatible API."""

    def __init__(self, *, time_scale: float = 1.0,
                 simulated_gpu_id: str = "replay-gpu-unbounded",
                 simulated_kv_bytes: int = 0) -> None:
        if time_scale < 0:
            raise ValueError("time_scale must be non-negative")
        if simulated_kv_bytes < 0:
            raise ValueError("simulated_kv_bytes must be non-negative")
        self.time_scale = time_scale
        self.simulated_gpu_id = simulated_gpu_id
        self.simulated_kv_bytes = simulated_kv_bytes

    def begin(self, action: ReplayAction, predicted_s: float) -> InferenceWait:
        if action.kind != "llm":
            raise ValueError("inference can begin only for an LLM action")
        wait = _TraceWait(
            request_id=action.action_id,
            predicted_s=predicted_s,
            simulated_s=action.duration_s * self.time_scale,
            time_scale=self.time_scale,
            metadata={
                "provider": "trace-replay",
                "gpu_id": self.simulated_gpu_id,
                "kv_cache_handle": None,
                "kv_bytes": self.simulated_kv_bytes,
            },
        )
        return wait
