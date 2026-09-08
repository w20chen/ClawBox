"""Small ClawTune span fixtures for model-gateway tests."""
import json


def llm_spans(messages, response, *, index=0, start=0, duration_ms=1, name="test-model"):
    identity = dict(schema_version=6, trace_id="test-run", span_id=f"model-{index}",
                    sequence_no=index, kind="llm", name=name)
    return [
        dict(identity, record_type="span_start", wall_time_ns=str(int(start * 1e9)),
             input={"messages": messages, "requested_args": None}),
        dict(identity, record_type="span_end", duration_ns=str(int(duration_ms * 1e6)),
             status={"code": "ok"}, output={"content": response}),
    ]


def write_spans(path, spans):
    path.write_text("".join(json.dumps(row) + "\n" for row in spans), encoding="utf-8")
