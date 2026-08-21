from __future__ import annotations

import hashlib
import base64
import hmac
import json
from datetime import datetime, timezone

from fastapi import Header, HTTPException
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

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
    return _grant_private_key().sign(_grant_payload(data)).hex()


def verify_grant(grant: ExecutionGrant) -> bool:
    try:
        _grant_public_key().verify(bytes.fromhex(grant.signature), _grant_payload(grant))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _grant_private_key() -> Ed25519PrivateKey:
    seed = hashlib.sha256(settings.grant_secret.encode("utf-8")).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def grant_public_key() -> str:
    raw = _grant_private_key().public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _grant_public_key() -> Ed25519PublicKey:
    encoded = settings.grant_public_key
    if not encoded:
        return _grant_private_key().public_key()
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    return Ed25519PublicKey.from_public_bytes(raw)
