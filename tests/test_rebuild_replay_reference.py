import base64
import json
from pathlib import Path
import subprocess
import sys

import yaml


def test_reference_capture_preserves_workload_and_rejects_changed_response(tmp_path):
    script = Path(__file__).parents[1] / "scripts/rebuild-replay-reference.py"
    original = tmp_path / "original.jsonl"
    row = {"type": "action", "action_type": "llm_call", "action_id": "original-id",
           "iteration": 0, "ts_start": 10, "ts_end": 12,
           "data": {"model": "recorded-model", "llm_latency_ms": 2000,
                    "raw_request": {"messages": [{"role": "user", "content": "old"}]},
                    "raw_response": {"content": "done"}}}
    original.write_text(json.dumps(row) + "\n")
    spec = tmp_path / "spec.yaml"
    spec.write_text(yaml.safe_dump({"execution": {"concurrency_levels": [1]},
                                    "workload": {"cases": [{}]}}))
    output = tmp_path / "capture"

    def invoke(phase, *extra, destination=output):
        return subprocess.run([sys.executable, str(script), phase,
                               "--original", str(original), "--output-dir", str(destination),
                               *map(str, extra)], capture_output=True, text=True)

    result = invoke("prepare", "--spec", spec)
    assert result.returncode == 0, result.stderr
    captured = json.loads((output / "rec-a-request-capture.jsonl").read_text())
    assert "raw_request" not in captured["data"]
    assert captured["data"]["raw_response"] == row["data"]["raw_response"]
    gateway = tmp_path / "gateway.json"
    payload = {"messages": [{"role": "user", "content": "healthy"}]}

    def record(content):
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})
        return {"replay_index": 0, "ready": True, "delivered": True, "error": "",
                "status_code": 200, "request_payload": payload,
                "content_type": "application/json", "response_b64": base64.b64encode(body.encode()).decode()}

    gateway.write_text(json.dumps([record("changed")]))
    assert invoke("finalize", "--gateway", gateway).returncode != 0
    assert not (output / "rec-a-healthy-reference.jsonl").exists()
    gateway.write_text(json.dumps([record("done")]))
    result = invoke("finalize", "--gateway", gateway)
    assert result.returncode == 0, result.stderr
    expected = json.loads(json.dumps(row))
    expected["data"]["raw_request"] = payload
    assert json.loads((output / "rec-a-healthy-reference.jsonl").read_text()) == expected
    assert json.loads(original.read_text()) == row
    assert invoke("finalize", "--gateway", gateway).returncode != 0  # no overwriting evidence
