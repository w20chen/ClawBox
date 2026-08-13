from __future__ import annotations
import os, subprocess, time, uuid
from pathlib import Path
from fastapi import HTTPException
from clawbox.common.config import settings
from clawbox.common.db import SessionLocal, ToolInstanceRow, WorkspaceBindingRow
from clawbox.common.http import post_json
from clawbox.common.models import ExecuteRequest, ToolInstance, ToolSpec
from clawbox.tool_agent.service import ToolAgent

class Controller:
    def __init__(self): self.local_agents: dict[str, ToolAgent] = {}
    def acquire(self, spec: ToolSpec) -> ToolInstance:
        with SessionLocal.begin() as db:
            binding = db.get(WorkspaceBindingRow, spec.workspace_id)
            if binding:
                if binding.tenant_id != spec.tenant_id: raise HTTPException(403, "workspace belongs to another tenant")
                row = db.get(ToolInstanceRow, binding.tool_pod_uid)
                if row and row.state == "READY": return self._model(row)
            uid = f"tool-{uuid.uuid4().hex[:16]}"
            if settings.controller_backend == "subprocess":
                workspace = str(Path(os.getenv("WORKSPACE_ROOT", "./.clawbox-workspaces")) / spec.tenant_id / spec.workspace_id)
                endpoint = f"inproc://{uid}"; backend_id = uid
                self.local_agents[uid] = ToolAgent(uid, spec.workspace_id, spec.tenant_id, workspace)
            elif settings.controller_backend == "docker":
                endpoint = f"http://{uid}:8090"
                try:
                    import docker
                    container = docker.from_env().containers.run(spec.image, detach=True, name=uid,
                        network=os.getenv("CONTROLLER_DOCKER_NETWORK", "clawbox_default"),
                        nano_cpus=spec.cpu_count * 1_000_000_000, mem_limit=spec.memory_bytes,
                        environment={"TOOL_POD_UID":uid,"TENANT_ID":spec.tenant_id,
                            "WORKSPACE_ID":spec.workspace_id,"CLAWBOX_SERVICE_TOKEN":settings.service_token,
                            "CLAWBOX_GRANT_SECRET":settings.grant_secret})
                    backend_id = container.id
                    self._wait_ready(endpoint)
                except Exception as exc:
                    try:
                        if "container" in locals(): container.remove(force=True)
                    except Exception: pass
                    raise HTTPException(503, f"Docker tool creation failed: {type(exc).__name__}") from exc
            else: raise HTTPException(500, "unsupported controller backend")
            row = ToolInstanceRow(tool_pod_uid=uid, tenant_id=spec.tenant_id, workspace_id=spec.workspace_id,
                backend=settings.controller_backend, backend_id=backend_id, endpoint=endpoint, state="READY")
            db.add(row); db.merge(WorkspaceBindingRow(workspace_id=spec.workspace_id,
                tenant_id=spec.tenant_id, tool_pod_uid=uid, endpoint=endpoint)); db.flush()
            return self._model(row)
    def execute(self, uid: str, request: ExecuteRequest) -> dict:
        with SessionLocal() as db:
            row = db.get(ToolInstanceRow, uid)
            if row is None or row.state != "READY": raise HTTPException(404, "tool instance unavailable")
            endpoint, backend = row.endpoint, row.backend
        if request.grant.tool_pod_uid != uid: raise HTTPException(403, "sticky routing mismatch")
        return self.local_agents[uid].execute(request) if backend == "subprocess" else post_json(
            f"{endpoint}/v1/execute", request.model_dump(mode="json"), timeout=360)
    def release(self, uid: str, destroy=False) -> ToolInstance:
        with SessionLocal.begin() as db:
            row = db.get(ToolInstanceRow, uid)
            if row is None: raise HTTPException(404, "tool instance not found")
            if destroy:
                if row.backend == "docker":
                    import docker
                    try: docker.from_env().containers.get(row.backend_id).remove(force=True)
                    except docker.errors.NotFound: pass
                self.local_agents.pop(uid, None); row.state = "DESTROYED"
            return self._model(row)
    @staticmethod
    def _model(row):
        return ToolInstance(tool_pod_uid=row.tool_pod_uid, tenant_id=row.tenant_id,
            workspace_id=row.workspace_id, endpoint=row.endpoint, backend=row.backend, state=row.state)
    @staticmethod
    def _wait_ready(endpoint: str) -> None:
        import httpx
        last_error: Exception | None = None
        for _ in range(60):
            try:
                response = httpx.get(f"{endpoint}/healthz", timeout=1)
                if response.status_code == 200: return
            except Exception as exc: last_error = exc
            time.sleep(.25)
        raise RuntimeError(f"tool agent readiness timed out: {type(last_error).__name__ if last_error else 'unhealthy'}")
