"""Bounded retries for read-only CubeSandbox API operations."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def retry_with_backoff(operation: Callable[[], T], *, label: str,
                       attempts: int = 5, initial_delay_s: float = 0.5,
                       max_delay_s: float = 8.0) -> T:
    """Retry an operation known by its caller to be idempotent."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    delay = initial_delay_s
    last: Exception | None = None
    for index in range(attempts):
        try:
            return operation()
        except Exception as exc:  # SDK exposes HTTP errors as several types.
            last = exc
            if index + 1 == attempts:
                break
            time.sleep(min(delay, max_delay_s))
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
