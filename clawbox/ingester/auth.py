from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_upload_token(task_id: str, secret: str, *, expires_at: int) -> str:
    payload = json.dumps(
        {"task_id": task_id, "exp": expires_at, "scope": "trace:write result:write"},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_encode(payload)}.{_encode(signature)}"


def verify_upload_token(token: str, task_id: str, secret: str) -> None:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = _decode(encoded_payload)
        signature = _decode(encoded_signature)
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
        claims = json.loads(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid upload token") from exc
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid upload token signature")
    if claims.get("task_id") != task_id:
        raise ValueError("upload token belongs to another task")
    if claims.get("scope") != "trace:write result:write":
        raise ValueError("upload token scope is invalid")
    if int(claims.get("exp", 0)) < int(time.time()):
        raise ValueError("upload token expired")
