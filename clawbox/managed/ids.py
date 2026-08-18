"""Server-generated identifiers (ADR-002).

- Run ID / Attempt ID: ULID (48-bit ms timestamp + 80-bit CSPRNG randomness,
  26-char Crockford base32) so they are time-ordered and collision-safe without
  a database round trip.
- Execution ID: UUID4 (Tool Protocol, per command execution).

The task name / prefix is display-only and must never be used as a durable
primary key.
"""

from __future__ import annotations

import secrets
import time
from uuid import uuid4

# Crockford base32 (no I, L, O, U).
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TS_CHARS = 10  # 48 bits of timestamp
_RAND_CHARS = 16  # 80 bits of randomness


def _encode(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(CROCKFORD[rem])
    return "".join(reversed(chars))


def new_ulid() -> str:
    """26-char ULID: 48-bit ms timestamp + 80-bit CSPRNG randomness."""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    return _encode(ts, _TS_CHARS) + _encode(secrets.randbits(80), _RAND_CHARS)


def new_run_id() -> str:
    return new_ulid()


def new_attempt_id() -> str:
    return new_ulid()


def new_execution_id() -> str:
    return str(uuid4())


def is_ulid(value: str) -> bool:
    return (
        len(value) == _TS_CHARS + _RAND_CHARS
        and all(c in CROCKFORD for c in value)
    )
