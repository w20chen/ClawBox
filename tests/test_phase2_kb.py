from datetime import timedelta
from clawbox.common.db import Base, engine, init_db
from clawbox.common.models import ExecutionIntent, Observation, utcnow
from clawbox.scheduler.service import Scheduler

def intent(tenant,eid,when): return ExecutionIntent(tenant_id=tenant,execution_id=eid,
    tool_name="exec",command="python -m pytest tests -q",repo_fingerprint="repo",
    workspace_id=f"ws-{tenant}",timestamp=when)
def test_tenant_overlay_and_duplicate_observation():
    Base.metadata.drop_all(engine);init_db(); scheduler=Scheduler(); now=utcnow()
    a=intent("tenant-a","e-a",now); b=intent("tenant-b","e-b",now)
    pa=scheduler.predict(a); pb=scheduler.predict(b); assert pa.sample_count==pb.sample_count
    obs=Observation(tenant_id="tenant-a",execution_id="e-a",start_time=now,
        end_time=now+timedelta(seconds=30),exit_code=0,cpu={"peak_cores":16},
        memory={"peak_bytes":1024**3},collection_quality="valid",collector_version="test",
        tool_image_digest="sha256:test",complete=True)
    assert scheduler.observation("e-a",obs,a)==(1,True)
    assert scheduler.observation("e-a",obs,a)==(1,False)
    assert scheduler.predict(b).kb_generation==0
def test_poisoned_observation_rejected():
    Base.metadata.drop_all(engine);init_db(); scheduler=Scheduler(); now=utcnow(); a=intent("tenant-a","e-a2",now)
    scheduler.predict(a)
    obs=Observation(tenant_id="tenant-b",execution_id="e-a2",start_time=now,end_time=now,
        exit_code=0,collection_quality="valid",collector_version="test",tool_image_digest="x",complete=True)
    import pytest
    with pytest.raises(Exception): scheduler.observation("e-a2",obs,a)

def test_failed_or_incomplete_observation_does_not_train():
    Base.metadata.drop_all(engine);init_db(); scheduler=Scheduler(); now=utcnow(); a=intent("tenant-a","failed",now)
    scheduler.predict(a)
    obs=Observation(tenant_id="tenant-a",execution_id="failed",start_time=now,end_time=now,
        exit_code=1,collection_quality="degraded",collector_version="test",tool_image_digest="x",complete=False)
    assert scheduler.observation("failed",obs,a)==(0,False)
