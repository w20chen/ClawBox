from __future__ import annotations

import json
from pathlib import Path

from clawbox.ingester import export


def test_export_jsonl_downloads_only_jsonl_and_preserves_paths(tmp_path, monkeypatch):
    manifest = {
        "paths": {
            "run/events.jsonl": {"bytes": 8},
            "tool-bridge.jsonl": {"bytes": 7},
            "tool-resource/sample.json": {"bytes": 99},
        }
    }
    responses = {
        "http://archive/v1/archive/task%2Fone/traces": json.dumps(manifest).encode(),
        "http://archive/v1/archive/task%2Fone/traces/run/events.jsonl": b'{"a":1}\n',
        "http://archive/v1/archive/task%2Fone/traces/tool-bridge.jsonl": b'bridge\n',
    }
    monkeypatch.setattr(export, "_request", lambda url, token: responses[url])

    output = tmp_path / "traces"
    paths = export.export_jsonl("http://archive", "secret", "task/one", output)

    assert [path.relative_to(output).as_posix() for path in paths] == [
        "run/events.jsonl",
        "tool-bridge.jsonl",
    ]
    assert (output / "run/events.jsonl").read_bytes() == b'{"a":1}\n'
    assert (output / "manifest.json").exists()
