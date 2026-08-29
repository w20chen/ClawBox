from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import httpx

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


class _ApiWait:
    def __init__(self, *, request_id: str, predicted_s: float,
                 future: Future[InferenceResult], metadata: dict[str, Any]) -> None:
        self._request_id = request_id
        self._predicted_s = predicted_s
        self._future = future
        self._metadata = metadata

    @property
    def request_id(self) -> str:
        return self._request_id

    @property
    def predicted_s(self) -> float:
        return self._predicted_s

    @property
    def estimated_wait_s(self) -> float:
        return self._predicted_s

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    def wait_ready(self) -> InferenceResult:
        return self._future.result()


class OpenAIInferenceProvider:
    """Issue a real OpenAI-compatible request while the VM may be evicted."""

    def __init__(self, *, base_url: str, api_key: str, model: str,
                 timeout_s: float = 600.0,
                 trust_env: bool = False,
                 post: Callable[..., httpx.Response] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self._client = None if post is not None else httpx.Client(trust_env=trust_env)
        self._post = post or self._client.post
        self._pool = ThreadPoolExecutor(thread_name_prefix="replay-inference")

    def begin(self, action: ReplayAction, predicted_s: float) -> InferenceWait:
        if action.kind != "llm":
            raise ValueError("inference can begin only for an LLM action")
        metadata = {"provider": "openai-compatible", "model": self.model}
        future = self._pool.submit(self._complete, action, predicted_s, metadata)
        return _ApiWait(
            request_id=action.action_id,
            predicted_s=predicted_s,
            future=future,
            metadata=metadata,
        )

    def close(self) -> None:
        self._pool.shutdown(wait=True)
        if self._client is not None:
            self._client.close()

    def _complete(self, action: ReplayAction, predicted_s: float,
                  metadata: dict[str, Any]) -> InferenceResult:
        messages = action.input
        if not isinstance(messages, list):
            messages = [{"role": "user", "content": str(messages or "")}]
        started = time.monotonic()
        response = self._post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "stream": False},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        elapsed = time.monotonic() - started
        return InferenceResult(
            request_id=action.action_id,
            predicted_s=predicted_s,
            simulated_s=elapsed,
            metadata={
                **metadata,
                "status_code": response.status_code,
                "usage": payload.get("usage") if isinstance(payload, dict) else None,
            },
        )
