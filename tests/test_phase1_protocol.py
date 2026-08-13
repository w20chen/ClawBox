from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from clawbox.common.models import ResourceRequest
from clawbox.allocator.app import app as allocator_app
from clawbox.controller.app import app as controller_app
from clawbox.node_agent.app import app as node_app
from clawbox.scheduler.app import app as scheduler_app

HEADERS={"Authorization":"Bearer development-only-token"}
def test_all_services_start_and_health():
    for app in (allocator_app,controller_app,node_app,scheduler_app):
        with TestClient(app) as client: assert client.get("/healthz").status_code==200
def test_allocator_protocol_rejects_command():
    with pytest.raises(ValidationError): ResourceRequest(tenant_id="t",execution_id="e",cpu_count=4,
        memory_bytes=1,expected_duration=1,command="secret")
def test_apis_require_identity():
    with TestClient(allocator_app) as client: assert client.get("/v1/capacity").status_code==401
