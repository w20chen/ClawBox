from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass, field
import time
from collections.abc import Callable
from typing import Any

from clawbox.replay.lifecycle import CommandResult, LifecycleError

from .client import CubeSandboxClient


_SAFE_EXECUTION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class ObservedCommand:
    result: CommandResult
    execution_id: str
    bridge_record: dict[str, Any]
    artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def telemetry_unavailable_reason(self) -> str | None:
        if self.bridge_record.get("telemetry_state") == "complete":
            return None
        return str(self.bridge_record.get("telemetry_error") or "Tool telemetry unavailable")


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

    def execute_observed(self, command: str, timeout_s: float, *,
                         execution_id: str) -> ObservedCommand:
        """Execute through Tool VM instrumentation using the Cube command API."""
        if not _SAFE_EXECUTION_ID.fullmatch(execution_id):
            raise ValueError("execution_id must be 1-128 safe ASCII identifier characters")
        envelope = (
            "__CBX_EXEC_1__"
            + json.dumps({"v": 1, "execution_id": execution_id}, separators=(",", ":"))
            + "\n" + command
        )
        encoded = base64.b64encode(envelope.encode()).decode()
        runner_timeout = max(1, math.ceil(timeout_s))
        result = self.execute(
            f"TOOL_EXEC_TIMEOUT_SECONDS={runner_timeout} "
            f"/usr/local/bin/tool-bridge --execute-base64 {encoded}",
            timeout_s + 7,
        )
        marker = "CLAWBOX_TELEMETRY_RECORD="
        record: dict[str, Any] | None = None
        stderr_lines: list[str] = []
        for line in result.stderr.splitlines():
            if line.startswith(marker):
                try:
                    candidate = json.loads(line.removeprefix(marker))
                    if candidate.get("execution_id") == execution_id:
                        record = candidate
                        continue
                except (TypeError, ValueError):
                    pass
            stderr_lines.append(line)
        cleaned = CommandResult(
            result.exit_code, result.stdout, "\n".join(stderr_lines), result.duration_s,
        )
        if record is None:
            record = {
                "execution_id": execution_id,
                "telemetry_state": "unavailable",
                "telemetry_error": "Tool telemetry runner emitted no valid record",
                "exit_code": result.exit_code,
            }
        artifacts: dict[str, str] = {}
        leaf = re.sub(r"[^A-Za-z0-9_.-]", "_", execution_id)
        candidates = {
            "cgroup_resource_v1": (
                f"/var/lib/clawtune/artifacts/tool-resource/cgroup-resource-{leaf}.json"
            ),
        }
        telemetry_path = record.get("telemetry_artifact")
        if isinstance(telemetry_path, str) and telemetry_path.startswith(
            "/var/lib/clawtune/artifacts/tool-resource/"
        ):
            candidates["clause_telemetry_v2"] = telemetry_path
        for kind, path in candidates.items():
            try:
                artifacts[kind] = self.client.read_file(self._handle(), path)
            except Exception:
                # The audit record carries the precise collector reason. A
                # missing optional artifact must never become fabricated data.
                continue
        return ObservedCommand(cleaned, execution_id, record, artifacts)

    def wait_ready(self, timeout_s: float) -> float:
        started = time.monotonic()
        result = self.execute("true", timeout_s)
        if result.exit_code != 0:
            raise LifecycleError(f"CubeSandbox readiness command failed: {result.stderr}")
        return time.monotonic() - started
