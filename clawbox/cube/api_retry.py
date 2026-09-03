"""Bounded retries for read-only CubeSandbox API operations."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def read_with_backoff(operation: Callable[[], T], *, label: str,
                      attempts: int = 5, initial_delay_s: float = 0.5,
                      max_delay_s: float = 8.0) -> T:
    """Retry a read-only operation with bounded exponential backoff."""
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
    raise RuntimeError(f"read-only Cube API operation failed after {attempts} attempts: {label}") from last
