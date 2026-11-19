"""Service authentication shared by the active tuning API."""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from .config import settings


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {settings.service_token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid service identity")
