import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_launcher():
    path = Path(__file__).resolve().parents[1] / "scripts/timed-clawtune-sidecar.py"
    spec = importlib.util.spec_from_file_location("timed_sidecar", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("failure", [False, True])
def test_client_timing_preserves_prediction_and_failure(failure, capsys, monkeypatch):
    launcher = load_launcher()
    ticks = iter([10., 12.5])
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(ticks))
    result = SimpleNamespace(tool_resource={"host_kb": {"prediction_receipt": "secret"}})
    error = RuntimeError("secret failure detail")
    request = SimpleNamespace(tool_name="exec", event_id="event", tool_call_id="call")
    calls = []

    def predict(value):
        calls.append(value)
        if failure:
            raise error
        return result

    receipt_key = ('session', 'call')
    predictor = SimpleNamespace(
        predict=predict,
        receipts={receipt_key: {'prediction_receipt': 'present'}},
        _key=lambda _request: receipt_key,
        prediction_timing=lambda: {"http_seconds": 2., "schema_seconds": .1},
    )
    launcher._time_host_predictions(predictor)
    if failure:
        with pytest.raises(RuntimeError) as raised:
            predictor.predict(request)
        assert raised.value is error
    else:
        assert predictor.predict(request) is result
    assert calls == [request]
    output = capsys.readouterr().out
    assert "secret" not in output
    row = json.loads(output)
    assert row["seconds"] == 2.5
    assert row["http_seconds"] == 2.
    assert row["schema_seconds"] == .1
    assert row["status"] == ("error" if failure else "ok")
    if not failure:
        assert row["remote_prediction"] is True


def test_non_tool_prediction_is_not_counted_as_host_request(capsys):
    launcher = load_launcher()
    expected = object()
    predictor = SimpleNamespace(predict=lambda request: expected)
    launcher._time_host_predictions(predictor)
    assert predictor.predict(SimpleNamespace(tool_name="read")) is expected
    assert capsys.readouterr().out == ""
