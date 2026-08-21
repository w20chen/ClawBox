from __future__ import annotations

import json
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select

from clawbox.common.auth import command_digest, sign_grant
from clawbox.common.config import settings
from clawbox.common.db import ExecutionRow, KBMetadataRow, ObservationRow, SessionLocal
from clawbox.common.http import delete_json, post_json
from clawbox.common.models import (ExecutionGrant, ExecutionIntent, ExecutionResult,
    LeaseState, Observation, ReleaseLease, ResourceLease, ResourcePrediction,
    ResourceRequest, ToolInstance, ToolSpec, utcnow)
from .kb import TenantKnowledgeBase


class Scheduler:
    def predict(self, intent: ExecutionIntent) -> ResourcePrediction:
        with SessionLocal.begin() as db:
            meta = db.get(KBMetadataRow, intent.tenant_id)
            if meta is None:
                meta = KBMetadataRow(tenant_id=intent.tenant_id, generation=0)
                db.add(meta); db.flush()
            execution = db.get(ExecutionRow, intent.execution_id)
            digest = command_digest(intent.command)
            if execution and (execution.tenant_id != intent.tenant_id or execution.command_digest != digest):
                raise HTTPException(409, "execution identity collision")
            if execution is None:
                db.add(ExecutionRow(execution_id=intent.execution_id, tenant_id=intent.tenant_id,
                    workspace_id=intent.workspace_id, command_digest=digest, state="PREDICTED",
                    intent_payload=intent.model_dump_json(), created_at=utcnow()))
            return TenantKnowledgeBase(meta.snapshot).predict(intent, meta.generation)

    def observation(self, execution_id: str, observation: Observation, intent: ExecutionIntent) -> tuple[int, bool]:
        if execution_id != observation.execution_id:
            raise HTTPException(400, "execution identity mismatch")
        with SessionLocal.begin() as db:
            execution = db.get(ExecutionRow, execution_id)
            if execution is None or execution.tenant_id != observation.tenant_id:
                raise HTTPException(403, "observation tenant does not own execution")
            existing = db.scalar(select(ObservationRow).where(
                ObservationRow.execution_id == execution_id,
                ObservationRow.observation_type == observation.observation_type,
                ObservationRow.version == observation.version,
            ))
            if existing is not None:
                meta = db.get(KBMetadataRow, observation.tenant_id)
                return (meta.generation if meta else 0), False
            trusted = observation.complete and observation.collection_quality == "valid" and observation.exit_code == 0
            row = ObservationRow(execution_id=execution_id, tenant_id=observation.tenant_id,
                observation_type=observation.observation_type, version=observation.version,
                payload=observation.model_dump_json(), trusted=trusted)
            db.add(row)
            db.flush()
            meta = db.get(KBMetadataRow, observation.tenant_id)
            if trusted:
                kb = TenantKnowledgeBase(meta.snapshot)
                kb.observe(intent, observation)
                meta.snapshot = kb.snapshot(); meta.generation += 1
                execution.state = "COMPLETED"
            else:
                execution.state = "FAILED"
            return meta.generation, trusted

    def stored_intent(self, execution_id: str) -> ExecutionIntent:
        with SessionLocal() as db:
            row = db.get(ExecutionRow, execution_id)
            if row is None:
                raise HTTPException(404, "execution not found")
            return ExecutionIntent.model_validate_json(row.intent_payload)

    def run(self, intent: ExecutionIntent) -> ExecutionResult:
        prediction = self.predict(intent)
        cpu_count = 4 if prediction.cpu_p90 <= 4 else 16 if prediction.cpu_p90 <= 16 else 32
        lease = ResourceLease.model_validate(post_json(f"{settings.allocator_url}/v1/leases",
            ResourceRequest(tenant_id=intent.tenant_id, execution_id=intent.execution_id,
                cpu_count=cpu_count, memory_bytes=prediction.memory_p90, preferred_numa=None,
                expected_duration=prediction.duration_p90).model_dump(mode="json")))
        tool = None
        observation = None
        tool_stopped = False
        stdout = stderr = ""
        try:
            tool = ToolInstance.model_validate(post_json(f"{settings.controller_url}/v1/tool-pods/acquire",
                ToolSpec(tenant_id=intent.tenant_id, execution_id=intent.execution_id,
                    workspace_id=intent.workspace_id, cpu_count=lease.cpu_count,
                    memory_bytes=lease.memory_bytes, numa_hint=lease.numa_hint,
                    image=intent.tool_image).model_dump(mode="json"), timeout=60))
            grant_data = dict(tenant_id=intent.tenant_id, execution_id=intent.execution_id,
                tool_pod_uid=tool.tool_pod_uid, workspace_id=intent.workspace_id,
                command_digest=command_digest(intent.command), lease_id=lease.lease_id,
                cpu_count=lease.cpu_count, numa_hint=lease.numa_hint,
                allocator_epoch=lease.allocator_epoch, fencing_token=lease.fencing_token,
                expires_at=min(lease.expires_at, utcnow() + timedelta(minutes=5)),
                nonce=str(uuid.uuid4()), signature="")
            grant_data["signature"] = sign_grant(grant_data)
            grant = ExecutionGrant(**grant_data)
            result = post_json(f"{settings.controller_url}/v1/tool-pods/{tool.tool_pod_uid}/execute",
                {"grant": grant.model_dump(mode="json"), "command": intent.command},
                timeout=max(30, prediction.duration_p90 * 2))
            observation = Observation.model_validate(result["observation"])
            stdout, stderr = result.get("stdout", ""), result.get("stderr", "")
            generation, _ = self.observation(intent.execution_id, observation, intent)
            released_tool = post_json(
                f"{settings.controller_url}/v1/tool-pods/{tool.tool_pod_uid}/release", {}, timeout=30
            )
            tool_stopped = released_tool.get("state") == "DESTROYED"
            if not tool_stopped:
                raise RuntimeError("controller did not confirm workload destruction")
            released = delete_json(f"{settings.allocator_url}/v1/leases/{lease.lease_id}",
                ReleaseLease(fencing_token=lease.fencing_token, workload_stopped=True).model_dump(), timeout=30)
            return ExecutionResult(execution_id=intent.execution_id, tenant_id=intent.tenant_id,
                prediction=prediction, lease=lease, tool=tool, observation=observation,
                kb_generation_before=prediction.kb_generation, kb_generation_after=generation,
                lease_final_state=released["state"], stdout=stdout, stderr=stderr)
        except Exception:
            if tool:
                try:
                    released_tool = post_json(
                        f"{settings.controller_url}/v1/tool-pods/{tool.tool_pod_uid}/release", {}
                    )
                    tool_stopped = released_tool.get("state") == "DESTROYED"
                except Exception: pass
            try: delete_json(f"{settings.allocator_url}/v1/leases/{lease.lease_id}",
                ReleaseLease(fencing_token=lease.fencing_token, workload_stopped=tool_stopped).model_dump())
            except Exception: pass
            raise
