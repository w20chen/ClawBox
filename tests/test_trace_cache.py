from __future__ import annotations

import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from trace_fixtures import llm_spans, write_spans

from clawbox.replay import trace as trace_module
from clawbox.replay.model_gateway import ModelGateway, _response_message


def test_trace_reuse_skips_json_decode_and_keeps_outer_lists_independent(tmp_path, monkeypatch):
    path = tmp_path / "trace.jsonl"
    write_spans(path, llm_spans([], {"content": "done"}))
    reads = []
    original = trace_module._read_jsonl

    def read(candidate):
        reads.append(candidate)
        return original(candidate)

    monkeypatch.setattr(trace_module, "_read_jsonl", read)
    first = trace_module.load_trace(path)
    second = trace_module.load_trace(path)
    assert reads == [path.resolve()]
    assert first is not second
    assert first[0] is second[0]
    first.clear()
    assert len(second) == len(trace_module.load_trace(path)) == 1


def test_modified_trace_invalidates_cached_actions(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_spans(path, llm_spans([], {"content": "before"}))
    first = trace_module.load_trace(path)
    old = path.stat()
    # Same-sized replacement still invalidates through its metadata.
    write_spans(path, llm_spans([], {"content": "after!"}))
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000))
    second = trace_module.load_trace(path)
    assert second[0] is not first[0]
    assert first[0].output == {"content": "before"}
    assert second[0].output == {"content": "after!"}


@pytest.mark.parametrize("stream", [False, True])
def test_shared_recording_keeps_process_bindings_timeouts_and_cursors_session_local(
    tmp_path, stream,
):
    def report(name, pid):
        return {"role": "tool", "tool_call_id": "background", "content":
                f"Command still running (session {name}, pid {pid})."}

    recorded = [report("recorded-process", 100)]
    output = {"content": None, "tool_calls": [
        {"id": "poll", "type": "function", "function": {
            "name": "process", "arguments": json.dumps({
                "action": "poll", "sessionId": "recorded-process",
            }),
        }},
        {"id": "exec", "type": "function", "function": {
            "name": "exec", "arguments": json.dumps({
                "command": "echo hello", "timeout": 30,
            }),
        }},
    ]}
    path = tmp_path / "trace.jsonl"
    write_spans(path, [*llm_spans(recorded, output),
                      *llm_spans(recorded, {"content": "done"}, index=1)])
    # This mirrors the worker's earlier duration/validation parse.
    loaded = trace_module.load_trace(path)
    original_input = copy.deepcopy(loaded[0].input)
    original_output = copy.deepcopy(loaded[0].output)
    gateways = [ModelGateway(
        tmp_path / f"session-{index}.json", mode="replay", trace=path,
        time_scale=0, request_namespace=f"session-{index}",
        tool_transport_timeout_s=300 + index,
    ) for index in range(2)]
    assert all(gateway.actions[0] is loaded[0] for gateway in gateways)
    assert gateways[0].actions is not gateways[1].actions

    def complete(index):
        return gateways[index].complete_http({
            "messages": [report(f"actual-{index}", 200 + index)], "stream": stream,
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(complete, range(2)))
    for index, (status, content_type, body, request_id) in enumerate(responses):
        assert status == 200
        calls = _response_message(content_type, body)["tool_calls"]
        assert json.loads(calls[0]["function"]["arguments"])["sessionId"] == f"actual-{index}"
        assert json.loads(calls[1]["function"]["arguments"])["timeout"] == 300 + index
        gateways[index].mark_delivery(request_id, delivered=True)
        assert gateways[index].records()[0]["replay_index"] == 0
    assert responses[0][3] != responses[1][3]
    assert loaded[0].input == original_input
    assert loaded[0].output == original_output

    gateways[0].complete_http({"messages": [report("actual-0", 200)], "stream": stream})
    assert gateways[0].logical_model_steps() == 2
    assert gateways[1].logical_model_steps() == 1
