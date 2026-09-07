import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def test_audit_distinguishes_exact_normalized_missing_and_changed(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "output_audit", Path(__file__).parents[1] / "scripts/audit-replay-outputs.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def message(call_id, content):
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    expected = {"messages": [message("exact", "ok"),
                             message("order", "src/a.py:1:x\nsrc/b.py:2:y"),
                             message("changed", "old"), message("missing", "not seen")]}
    actual = {"messages": [message("exact", "ok"),
                           message("order", "src/b.py:2:y\nsrc/a.py:1:x"),
                           message("changed", "new")]}
    monkeypatch.setattr(module, "load_trace", lambda _: [SimpleNamespace(kind="llm", input=expected)])
    gateway = tmp_path / "gateway.json"
    gateway.write_text(json.dumps([{"replay_index": 0, "request_payload": actual,
                                    "replay_input_match": True, "delivered": True}]))
    result = module.audit(tmp_path / "trace.jsonl", gateway)
    assert result["tool_output_counts"] == {"exact": 1, "normalized": 1, "different": 1, "missing": 1}
    assert not result["complete"]
    gateway.write_text(json.dumps([{"replay_index": 0, "request_payload": expected,
                                    "replay_input_match": True, "delivered": True}]))
    assert module.audit(tmp_path / "trace.jsonl", gateway)["complete"]
