from datetime import timedelta
import pytest
from clawbox.common.auth import command_digest, sign_grant
from clawbox.common.db import Base, SessionLocal, TenantRow, engine, init_db
from clawbox.common.models import ExecuteRequest, ExecutionGrant, ResourceRequest, ToolSpec, utcnow
from clawbox.allocator.service import Allocator
from clawbox.controller.service import Controller

def grant(tool, lease, command, nonce="n"):
    data=dict(tenant_id=tool.tenant_id,execution_id=lease.execution_id,tool_pod_uid=tool.tool_pod_uid,
        workspace_id=tool.workspace_id,command_digest=command_digest(command),lease_id=lease.lease_id,
        cpu_count=lease.cpu_count,numa_hint=lease.numa_hint,allocator_epoch=lease.allocator_epoch,
        fencing_token=lease.fencing_token,expires_at=utcnow()+timedelta(minutes=1),nonce=nonce,signature="")
    data["signature"]=sign_grant(data);return ExecutionGrant(**data)
def test_real_command_sticky_and_replay():
    Base.metadata.drop_all(engine);init_db(); alloc=Allocator(); ctl=Controller()
    lease=alloc.create(ResourceRequest(tenant_id="tenant-a",execution_id="chain-1",cpu_count=4,
        memory_bytes=1024,expected_duration=1))
    tool=ctl.acquire(ToolSpec(tenant_id="tenant-a",execution_id="chain-1",workspace_id="ws-a",
        cpu_count=4,memory_bytes=1024,image="unused"))
    command="python -c \"print('clawbox-ok')\""; request=ExecuteRequest(grant=grant(tool,lease,command),command=command)
    result=ctl.execute(tool.tool_pod_uid,request);assert result["stdout"].strip()=="clawbox-ok"
    assert result["observation"]["execution_id"]=="chain-1"
    with pytest.raises(Exception):ctl.execute(tool.tool_pod_uid,request)
def test_quota_prevents_double_capacity():
    Base.metadata.drop_all(engine);init_db();alloc=Allocator()
    with SessionLocal.begin() as db:db.add(TenantRow(tenant_id="limited",cpu_quota=16,concurrency_quota=2))
    alloc.create(ResourceRequest(tenant_id="limited",execution_id="one",cpu_count=16,memory_bytes=1,expected_duration=1))
    with pytest.raises(Exception):alloc.create(ResourceRequest(tenant_id="limited",execution_id="two",cpu_count=16,memory_bytes=1,expected_duration=1))

def test_expired_grant_is_rejected():
    Base.metadata.drop_all(engine);init_db();alloc=Allocator();ctl=Controller()
    lease=alloc.create(ResourceRequest(tenant_id="tenant-a",execution_id="expired",cpu_count=4,memory_bytes=1,expected_duration=1))
    tool=ctl.acquire(ToolSpec(tenant_id="tenant-a",execution_id="expired",workspace_id="expired-ws",cpu_count=4,memory_bytes=1,image="unused"))
    data=dict(tenant_id="tenant-a",execution_id="expired",tool_pod_uid=tool.tool_pod_uid,
        workspace_id="expired-ws",command_digest=command_digest("echo no"),lease_id=lease.lease_id,
        cpu_count=4,numa_hint=lease.numa_hint,allocator_epoch=lease.allocator_epoch,
        fencing_token=lease.fencing_token,expires_at=utcnow()-timedelta(seconds=1),nonce="old",signature="")
    data["signature"]=sign_grant(data)
    with pytest.raises(Exception):ctl.execute(tool.tool_pod_uid,ExecuteRequest(grant=ExecutionGrant(**data),command="echo no"))
