from __future__ import annotations

import base64
import hashlib
import time

from fastapi.testclient import TestClient

from clawbox.common.config import settings
from clawbox.common.db import Base, engine, init_db
from clawbox.ingester.app import app
from clawbox.ingester.auth import create_upload_token


def test_trace_and_result_ingestion_is_idempotent_immutable_and_receipted():
    Base.metadata.drop_all(engine)
    init_db()
    task_id = "cell-ingest"
    token = create_upload_token(task_id, settings.ingest_secret, expires_at=int(time.time()) + 60)
    upload_headers = {"Authorization": f"Bearer {token}"}
    archive_headers = {"Authorization": f"Bearer {settings.service_token}"}
    data = b"trace-data"
    digest = hashlib.sha256(data).hexdigest()
    chunk = {
        "chunk_id": "a" * 64,
        "relative_path": "events.jsonl",
        "offset": 0,
        "sha256": digest,
        "data_base64": base64.b64encode(data).decode(),
    }
    final = {
        "chunk_id": "b" * 64,
        "relative_path": ".final",
        "offset": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
        "data_base64": "",
        "final": True,
    }

    with TestClient(app) as client:
        assert client.post(f"/v1/tasks/{task_id}/traces", json=chunk, headers=upload_headers).status_code == 200
        assert client.post(f"/v1/tasks/{task_id}/traces", json=chunk, headers=upload_headers).status_code == 200

        collision = dict(chunk, chunk_id="c" * 64, data_base64=base64.b64encode(b"other").decode())
        collision["sha256"] = hashlib.sha256(b"other").hexdigest()
        assert client.post(
            f"/v1/tasks/{task_id}/traces", json=collision, headers=upload_headers,
        ).status_code == 409

        result = {"status": "succeeded", "final_answer": "done", "patch": "diff"}
        assert client.post(f"/v1/tasks/{task_id}/result", json=result, headers=upload_headers).status_code == 200
        changed = dict(result, final_answer="different")
        assert client.post(f"/v1/tasks/{task_id}/result", json=changed, headers=upload_headers).status_code == 409
        assert client.post(f"/v1/tasks/{task_id}/traces", json=final, headers=upload_headers).status_code == 200

        receipt = client.get(f"/v1/tasks/{task_id}/receipt", headers=upload_headers).json()
        assert receipt["complete"] is True
        assert receipt["trace_chunks"] == 2
        archive = client.get(
            f"/v1/archive/{task_id}/traces/events.jsonl", headers=archive_headers,
        )
        assert archive.content == data
