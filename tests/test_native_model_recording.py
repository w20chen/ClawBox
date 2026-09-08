"""Replay the actual ClawTune writer's output, without a conversion step."""
import json
from datetime import datetime, timezone

import pytest

from clawbox.replay.model_gateway import ModelGateway
from clawbox.replay.trace import find_recordings, load_trace


def test_clawtune_writer_output_replays_without_modification(tmp_path):
    from clawtune_sidecar.trace import AgentTestBenchTraceWriter
    from clawtune_sidecar.contracts.models import ModelEvent

    messages = [{"role": "user", "content": "Run the tests."}]
    reply = {"content": "", "tool_calls": [{"id": "call-1", "type": "function",
             "function": {"name": "exec", "arguments": '{"command":"pytest -q"}'}}]}
    common = dict(schema_version="clawtune.v1", plugin_version="test", run_id="run-a",
                  session_id="session-a", session_key=None, agent_id="main",
                  call_id="model-1", provider="test", model="test-model",
                  runtime_id="session-a", gateway_id="session-a",
                  outcome="completed")
    writer = AgentTestBenchTraceWriter(tmp_path / "native")
    writer.record_model(ModelEvent(**common, event_id="start", event_type="model_call_started",
                                  occurred_at="2026-09-08T00:00:00Z"))
    started = datetime(2026, 9, 8, tzinfo=timezone.utc).timestamp()
    writer.record_llm_proxy_call(
        runtime_id="session-a", action_id="proxy-1", provider="test", model="test-model",
        messages_in=messages, content=reply, raw_request={"messages": messages},
        raw_response={"choices": [{"message": reply}]}, ts_start=started,
        ts_end=started + 1, status_code=200, stream=False,
    )
    writer.record_model(ModelEvent(**common, event_id="end", event_type="model_call_ended",
                                  occurred_at="2026-09-08T00:00:01Z", duration_ms=1000,
                                  raw_output=None))
    writer.close()
    trace, = (tmp_path / "native").rglob("*.jsonl")
    before = trace.read_bytes()
    assert find_recordings([trace], "session-a") == [trace]
    assert find_recordings([trace], "other-session") == []
    actions = load_trace(trace)
    assert len(actions) == 1
    assert actions[0].duration_s == 1
    gateway = ModelGateway(tmp_path / "gateway.json", mode="replay", trace=trace, time_scale=0)
    status, _, body, request_id = gateway.complete_http({"model": "test-model", "messages": messages})
    gateway.mark_delivery(request_id, delivered=True)
    assert status == 200
    assert json.loads(body)["choices"][0]["message"]["tool_calls"] == reply["tool_calls"]
    assert gateway.replay_completeness()["complete"]
    assert trace.read_bytes() == before


def test_action_recordings_are_not_supported(tmp_path):
    trace = tmp_path / "unsupported.jsonl"
    trace.write_text(json.dumps({"type": "action", "action_type": "llm_call"}) + "\n")
    with pytest.raises(ValueError, match="ClawTune schema-6"):
        load_trace(trace)
