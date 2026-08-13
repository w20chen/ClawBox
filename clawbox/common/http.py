from __future__ import annotations

import httpx

from .config import settings


def service_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.service_token}"}


def post_json(url: str, payload: dict, timeout: float = 30.0) -> dict:
    response = httpx.post(url, json=payload, headers=service_headers(), timeout=timeout)
    response.raise_for_status()
    return response.json()


def delete_json(url: str, payload: dict, timeout: float = 30.0) -> dict:
    response = httpx.request("DELETE", url, json=payload, headers=service_headers(), timeout=timeout)
    response.raise_for_status()
    return response.json()
