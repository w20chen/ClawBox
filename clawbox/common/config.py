"""Environment settings for the active tuning service."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    service_token: str = os.getenv("CLAWBOX_SERVICE_TOKEN", "development-only-token")
    ingest_secret: str = os.getenv("CLAWBOX_INGEST_SECRET", "development-only-ingest-secret")
    kb_ingest_secret: str = os.getenv("CLAWBOX_KB_INGEST_SECRET", "")
    clawtune_revision: str = os.getenv("CLAWTUNE_REVISION", "main")


settings = Settings()
