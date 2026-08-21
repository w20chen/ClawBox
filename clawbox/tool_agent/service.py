from __future__ import annotations
import os, signal, subprocess, threading, time
from pathlib import Path
from fastapi import HTTPException
from clawbox.common.auth import command_digest, verify_grant
from clawbox.common.models import ExecuteRequest, Observation, utcnow

class ToolAgent:
    def __init__(self, tool_pod_uid=None, workspace_id=None, tenant_id=None, workspace=None):
        self.tool_pod_uid = tool_pod_uid or os.getenv("TOOL_POD_UID", "local-tool")
        self.workspace_id = workspace_id or os.getenv("WORKSPACE_ID", "local-workspace")
        self.tenant_id = tenant_id or os.getenv("TENANT_ID", "local-tenant")
        self.workspace = workspace or os.getenv("WORKSPACE_PATH", "/workspace")
        self._used_nonces: set[str] = set(); self._lock = threading.Lock()
        self._running: set[subprocess.Popen] = set()

    def execute(self, request: ExecuteRequest) -> dict:
        grant = request.grant
        if not verify_grant(grant): raise HTTPException(403, "invalid grant signature")
        if grant.expires_at <= utcnow(): raise HTTPException(403, "expired execution grant")
        if grant.tool_pod_uid != self.tool_pod_uid: raise HTTPException(403, "grant targets another tool instance")
        if grant.tenant_id != self.tenant_id or grant.workspace_id != self.workspace_id:
            raise HTTPException(403, "grant ownership mismatch")
        if command_digest(request.command) != grant.command_digest: raise HTTPException(403, "command digest mismatch")
        with self._lock:
            if grant.nonce in self._used_nonces: raise HTTPException(409, "replayed execution grant")
            self._used_nonces.add(grant.nonce)
        Path(self.workspace).mkdir(parents=True, exist_ok=True)
        start = utcnow(); mono = time.monotonic()
        process = subprocess.Popen(
            request.command, cwd=self.workspace, shell=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        with self._lock:
            self._running.add(process)
        timed_out = False
        try:
            stdout, stderr = process.communicate(
                timeout=int(os.getenv("TOOL_EXEC_TIMEOUT_SECONDS", "300"))
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(process)
            stdout, stderr = process.communicate()
        finally:
            with self._lock:
                self._running.discard(process)
        elapsed = time.monotonic() - mono; end = utcnow()
        # Docker provides the hard CPU/memory boundary. This portable collector
        # records wall/process semantics; cgroup/eBPF collectors can replace it.
        observation = Observation(tenant_id=self.tenant_id, execution_id=grant.execution_id,
            start_time=start, end_time=end, exit_code=124 if timed_out else process.returncode,
            cpu={"wall_seconds": elapsed}, memory={}, disk={}, network={},
            process_events=[{"type": "exec"}, {"type": "timeout" if timed_out else "exit", "code": process.returncode}],
            # This portable path has no independent cgroup/eBPF counters.  It
            # must never masquerade wall time or placeholder RSS as valid
            # resource telemetry; native cell collection supplies valid data.
            collection_quality="degraded", collector_version="clawbox-process-v1",
            tool_image_digest=os.getenv("TOOL_IMAGE_DIGEST", "unknown"), complete=not timed_out,
            cgroup=f"/sys/fs/cgroup/claw/{grant.execution_id}" if os.name != "nt" else None)
        return {"stdout": stdout, "stderr": stderr,
                "observation": observation.model_dump(mode="json")}

    def stop(self) -> None:
        with self._lock:
            running = list(self._running)
        for process in running:
            self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
