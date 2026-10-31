"""ClawBox delivery settings; the upstream ClawTune configuration is unchanged."""
from dataclasses import dataclass, replace
import math
import os

from clawtune_sidecar.config import SidecarConfig


@dataclass(frozen=True)
class HostSidecarConfig(SidecarConfig):
    host_kb_endpoint: str | None = None
    host_kb_token: str | None = None
    host_kb_session_id: str | None = None
    host_kb_timeout_seconds: float = 30.

    def __post_init__(self):
        super().__post_init__()
        if self.host_kb_endpoint and (not self.host_kb_token or not self.host_kb_session_id):
            raise ValueError('host KB endpoint requires token and session ID')
        if not math.isfinite(self.host_kb_timeout_seconds) or self.host_kb_timeout_seconds <= 0:
            raise ValueError('host KB timeout must be positive finite seconds')

    @classmethod
    def from_env(cls):
        return replace(super().from_env(),
            host_kb_endpoint=os.getenv('CLAWTUNE_HOST_KB_ENDPOINT') or None,
            host_kb_token=os.getenv('CLAWTUNE_HOST_KB_TOKEN') or None,
            host_kb_session_id=os.getenv('CLAWTUNE_HOST_KB_SESSION_ID') or None,
            host_kb_timeout_seconds=float(os.getenv('CLAWTUNE_HOST_KB_TIMEOUT_SECONDS', '30')))
