from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from clawbox.replay.lifecycle import CommandResult, LifecycleError

from .client import CubeSandboxClient


class CubeCommandExecutor:
    def __init__(self, client: CubeSandboxClient, sandbox: Any | Callable[[], Any],
                 *, cwd: str = "/workspace") -> None:
        self.client = client
        self._sandbox = sandbox
        self.cwd = cwd

    def _handle(self) -> Any:
        handle = self._sandbox() if callable(self._sandbox) else self._sandbox
        if handle is None:
            raise LifecycleError("CubeSandbox has not been started")
        return handle

    def execute(self, command: str, timeout_s: float) -> CommandResult:
        started = time.monotonic()
        result = self.client.run_command(
            self._handle(), command, timeout_s=timeout_s, cwd=self.cwd,
        )
        return CommandResult(
            exit_code=int(result.exit_code),
            stdout=str(result.stdout),
            stderr=str(result.stderr),
            duration_s=time.monotonic() - started,
        )

    def wait_ready(self, timeout_s: float) -> float:
        started = time.monotonic()
        result = self.execute("true", timeout_s)
        if result.exit_code != 0:
            raise LifecycleError(f"CubeSandbox readiness command failed: {result.stderr}")
        return time.monotonic() - started
