"""Bounded retries for read-only CubeSandbox API operations."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def retry_with_backoff(operation: Callable[[], T], *, label: str,
                       attempts: int = 5, initial_delay_s: float = 0.5,
                       max_delay_s: float = 8.0,
                       before_retry: Callable[[], None] | None = None,
                       deadline_s: float | None = None) -> T:
    """Retry an operation known by its caller to be idempotent."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    if deadline_s is not None and deadline_s <= 0:
        raise ValueError("retry deadline must be positive")
    delay = initial_delay_s
    deadline = time.monotonic() + deadline_s if deadline_s is not None else None
    last: Exception | None = None
    for index in range(attempts):
        try:
            return operation()
        except Exception as exc:  # SDK exposes HTTP errors as several types.
            last = exc
            if index + 1 == attempts:
                break
            sleep_seconds = min(delay, max_delay_s)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sleep_seconds = min(sleep_seconds, remaining)
            time.sleep(sleep_seconds)
            if deadline is not None and time.monotonic() >= deadline:
                break
            if before_retry is not None:
                before_retry()
                if deadline is not None and time.monotonic() >= deadline:
                    break
            delay = min(delay * 2, max_delay_s)
    raise RuntimeError(
        f"idempotent Cube API operation failed after {attempts} attempts: {label}"
    ) from last


def read_with_backoff(operation: Callable[[], T], *, label: str,
                      attempts: int = 5, initial_delay_s: float = 0.5,
                      max_delay_s: float = 8.0) -> T:
    """Retry a read-only operation with bounded exponential backoff."""
    try:
        return retry_with_backoff(
            operation, label=label, attempts=attempts,
            initial_delay_s=initial_delay_s, max_delay_s=max_delay_s,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"read-only Cube API operation failed after {attempts} attempts: {label}"
        ) from exc.__cause__
