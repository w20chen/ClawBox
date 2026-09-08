import json

import pytest

from clawbox.replay.model_gateway import ModelGateway
from clawbox.replay.process_sessions import bind_process_sessions, rebind_process_sessions
from trace_fixtures import llm_spans, write_spans


def report(session, pid):
    return {"role": "tool", "tool_call_id": "background-call", "content":
            f"Command still running (session {session}, pid {pid}). Use process for follow-up."}


def test_recorded_poll_is_bound_to_real_openclaw_session_without_editing_trace(tmp_path):
    before, actual = [report("warm-crustacean", 2727)], [report("warm-daisy", 2720)]
    poll = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "poll-call", "type": "function", "function": {"name": "process",
        "arguments": json.dumps({"action": "poll", "sessionId": "warm-crustacean"})}}]}
    result = {"role": "tool", "tool_call_id": "poll-call", "content": "done"}
    path = tmp_path / "native.jsonl"
    write_spans(path, [*llm_spans(before, poll),
                      *llm_spans([*before, poll, result], {"content": "finished"}, index=1)])
    original = path.read_bytes()
    gateway = ModelGateway(tmp_path / "gateway.json", mode="replay", trace=path, time_scale=0)
    status, _, body, request_id = gateway.complete_http({"messages": actual})
    assert status == 200
    observed_poll = json.loads(body)["choices"][0]["message"]
    arguments = json.loads(observed_poll["tool_calls"][0]["function"]["arguments"])
    assert arguments == {"action": "poll", "sessionId": "warm-daisy"}
    gateway.mark_delivery(request_id, delivered=True)
    status, _, body, request_id = gateway.complete_http({"messages": [*actual, observed_poll, result]})
    assert status == 200
    gateway.mark_delivery(request_id, delivered=True)
    assert gateway.replay_completeness()["complete"] is True
    assert path.read_bytes() == original
    assert gateway.records()[0]["replay_process_sessions"]["warm-crustacean"]["session_id"] == "warm-daisy"


def test_bindings_require_matching_tool_identity_and_preserve_other_content():
    before, after = [report("old-name", 10)], [report("new-name", 20)]
    bindings = bind_process_sessions(before, after, {})
    assert rebind_process_sessions(before, bindings) == after
    assert rebind_process_sessions("unrelated pid 10; failure", bindings) == "unrelated pid 10; failure"
    after[0]["tool_call_id"] = "different-call"
    assert bind_process_sessions(before, after, {}) == {}
    with pytest.raises(ValueError, match="changed identity"):
        bind_process_sessions(before, [report("another-name", 30)], bindings)


def test_binding_does_not_hide_a_different_tool_result(tmp_path):
    path = tmp_path / "native.jsonl"
    write_spans(path, llm_spans([report("old-name", 10)], {"content": "ok"}))
    gateway = ModelGateway(tmp_path / "gateway.json", mode="replay", trace=path, time_scale=0)
    actual = report("new-name", 20)
    actual["content"] += " ERROR: different execution outcome"
    with pytest.raises(ValueError, match="diverged"):
        gateway.complete({"messages": [actual]})
