from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from fastapi import Header, HTTPException

from .config import settings
from .models import ExecutionGrant


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {settings.service_token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid service identity")


def command_digest(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _grant_payload(grant: ExecutionGrant | dict[str, object]) -> bytes:
    data = grant.model_dump(mode="json") if isinstance(grant, ExecutionGrant) else dict(grant)
    data.pop("signature", None)
    data = _canonical(data)
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def _canonical(value):
    if isinstance(value, datetime):
        normalized = value.astimezone(timezone.utc).isoformat()
        return normalized.replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def sign_grant(data: dict[str, object]) -> str:
    return hmac.new(settings.grant_secret.encode(), _grant_payload(data), hashlib.sha256).hexdigest()


def verify_grant(grant: ExecutionGrant) -> bool:
    expected = hmac.new(
        settings.grant_secret.encode(), _grant_payload(grant), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, grant.signature)
