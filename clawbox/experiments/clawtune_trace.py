"""ClawTune-compatible observations for commands executed by CubeSandbox.

The Worker is the trust boundary: OpenClaw requests a tool call, CubeSandbox
executes it, and only the Worker records the authoritative result.  ClawTune
consumes these records offline; it never receives command execution or sandbox
lifecycle authority.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from clawbox.replay.lifecycle import CommandResult


class ClawTuneTraceWriter:
    """Write exact-ID v6 spans and matching Cube tool-bridge records."""

    def __init__(self, root: Path, *, run_id: str, session_id: str,
                 repo_fingerprint: str | None = None) -> None:
        self.trace_path = root / "traces" / f"{session_id}.jsonl"
        self.bridge_path = root / "tool-bridge.jsonl"
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.session_id = session_id
        self.repo_fingerprint = repo_fingerprint
        self._sequence = 0
        self._lock = Lock()

    def record(self, command: str, result: CommandResult) -> str:
        """Record one completed Cube command and return its execution ID."""
        execution_id = f"cube-{uuid.uuid4().hex}"
        digest = hashlib.sha256(command.encode()).hexdigest()
        now_ns = time.time_ns()
        duration_ns = max(0, int(result.duration_s * 1_000_000_000))
        status = "ok" if result.exit_code == 0 else (
            "timeout" if result.exit_code == 124 else "error"
        )
        with self._lock:
            sequence = self._sequence
            self._sequence += 1
            span = {
                "schema_version": 6,
                "record_type": "span_end",
                "trace_id": f"trace-{self.session_id}",
                "span_id": f"span-{execution_id}",
                "parent_span_id": None,
                "session_id": self.session_id,
                "run_id": self.run_id,
                "agent_id": self.session_id,
                "sequence_no": sequence,
                "kind": "tool",
                "name": "cube_shell",
                "wall_time_ns": str(now_ns),
                "monotonic_time_ns": str(time.monotonic_ns()),
                "duration_ns": str(duration_ns),
                "duration_sec": str(result.duration_s),
                "repo": self.repo_fingerprint,
                "status": {"code": status, "message": None},
                "output": {"exit_code": result.exit_code, "result": None},
                "execution": {
                    "mode": "clawbox_cube_worker",
                    "execution_id": execution_id,
                    "requested_command": command,
                    "payload_command": command,
                    "command_digest": digest,
                },
                # The Worker observes the entire Cube RPC wall-time window.
                # CPU/RSS remain null until an independent Cube collector is
                # available; the latency observation itself is complete.
                "resources": {
                    "attribution_status": "latency_only",
                    "scope": "cube_rpc",
                    "quality": "complete",
                    "monitor_start_wall_time_ns": str(now_ns - duration_ns),
                    "monitor_end_wall_time_ns": str(now_ns),
                    "coverage_ratio": 1.0,
                    "coverage_reason": "cube_rpc_full_window",
                    "action_duration_ns": str(duration_ns),
                    "cpu_time_s": None,
                    "cpu_utilization_avg_cores": None,
                    "rss_peak_bytes": None,
                    "memory_rss_bytes_after": None,
                },
            }
            bridge = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cell_id": None,
                "task_id": self.session_id,
                "execution_id": execution_id,
                "execution_source": "runtime-envelope",
                "command_sha256": digest,
                "command_bytes": len(command.encode()),
                "duration_ms": max(0, round(result.duration_s * 1000)),
                "exit_code": result.exit_code,
                "timed_out": result.exit_code == 124,
                "stdout_bytes": len(result.stdout.encode()),
                "stderr_bytes": len(result.stderr.encode()),
                "output_truncated": False,
            }
            self._append(self.trace_path, span)
            self._append(self.bridge_path, bridge)
        return execution_id

    @staticmethod
    def _append(path: Path, value: dict) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()

