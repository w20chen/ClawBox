import importlib.util
import json
from pathlib import Path


def test_status_keeps_partial_live_output_explicit(tmp_path):
    spec = importlib.util.spec_from_file_location("study_status", Path(__file__).resolve().parents[1] / "scripts/study-status.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = tmp_path / "runs/run-00/events"
    events.mkdir(parents=True)
    (events / "arm.jsonl").write_text(json.dumps({"event": "session_complete", "session_id": "a", "valid": True}) + '\n{"event":')
    result = module.status(tmp_path)
    assert "Finished arm records: 0; fully validated: 0" in result
    assert "Validated sessions: 1" in result
    assert "prefix run includes one final synthetic-stop" in result
